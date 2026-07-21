#!/usr/bin/env python3
"""
Deterministic Garmin -> marathon plan sync, designed to run unattended
inside GitHub Actions (no Claude reasoning in this path). Operates directly
on index.html (the single source of truth: PLAN and FITNESS_LOG are
embedded JS/JSON in the file), matching new Garmin activities to open plan
days, applying the ahead-of-plan mileage rule, appending a daily fitness
snapshot, and rewriting the file in place. The workflow that calls this
script is responsible for git add/commit/push.

Env vars:
  GARMIN_TOKENSTORE_B64 - base64 of a tar.gz of the garminconnect tokenstore
                           directory (garmin_tokens.json + .mfa_state.json)

Exits non-zero with GARMIN_AUTH_ERROR on the stderr if the cached session
can't be used (expired/revoked) -- this needs a one-time human re-auth,
same as the original local setup.
"""
import base64
import io
import json
import os
import re
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import garminconnect

TZ = ZoneInfo("America/New_York")
METERS_PER_MILE = 1609.344
METERS_PER_FOOT = 0.3048
HTML_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")

RUNNING_TYPES = {"running", "treadmill_running", "trail_running", "track_running", "virtual_run", "street_running"}

TRAINING_STATUS_LABELS = {
    0: "No status",
    1: "Detraining",
    2: "Recovery",
    3: "Maintaining",
    4: "Productive",
    5: "Peaking",
    6: "Overreaching",
    7: "Unproductive",
}


def load_tokenstore():
    b64 = os.environ.get("GARMIN_TOKENSTORE_B64")
    if not b64:
        print("GARMIN_AUTH_ERROR: GARMIN_TOKENSTORE_B64 secret not set", file=sys.stderr)
        sys.exit(1)
    tmp = tempfile.mkdtemp(prefix="garmin_ts_")
    raw = base64.b64decode(b64)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(tmp)
    return tmp


def login():
    tokenstore = load_tokenstore()
    try:
        client = garminconnect.Garmin()
        client.login(tokenstore=tokenstore)
        return client
    except Exception as e:
        print(f"GARMIN_AUTH_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        print(
            "Cached Garmin session could not be used (expired/revoked). "
            "Needs a one-time human re-auth (login + MFA) to refresh the "
            "GARMIN_TOKENSTORE_B64 secret -- ask Claude in chat to do this.",
            file=sys.stderr,
        )
        sys.exit(1)


def fetch_activities(client):
    raw = client.get_activities(0, 20)
    activities = []
    for a in raw:
        dist_m = a.get("distance") or 0
        start = a.get("startTimeLocal", "")
        date = start.split(" ")[0] if start else ""
        activities.append(
            {
                "id": a.get("activityId"),
                "type": (a.get("activityType") or {}).get("typeKey", ""),
                "date": date,
                "title": a.get("activityName", "") or "Activity",
                "distance_mi": round(dist_m / METERS_PER_MILE, 2) if dist_m else 0.0,
                "duration_min": round((a.get("duration") or 0) / 60),
                "avg_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
            }
        )
    return activities


def fetch_fitness_metrics(client, date_str):
    """Best-effort daily fitness snapshot. Each sub-metric fails independently
    so one missing data point doesn't blank out the whole entry."""
    entry = {"date": date_str}

    try:
        race = client.get_race_predictions()
        entry["predictedMarathonSec"] = race.get("timeMarathon")
        entry["predictedHalfSec"] = race.get("timeHalfMarathon")
    except Exception:
        pass

    try:
        mm = client.get_max_metrics(date_str)
        if mm:
            entry["vo2max"] = mm[0].get("generic", {}).get("vo2MaxValue")
    except Exception:
        pass

    try:
        ts = client.get_training_status(date_str)
        latest = (ts or {}).get("mostRecentTrainingStatus", {}).get("latestTrainingStatusData", {})
        if latest:
            first = next(iter(latest.values()))
            code = first.get("trainingStatus")
            entry["trainingStatus"] = TRAINING_STATUS_LABELS.get(code, f"Status {code}")
    except Exception:
        pass

    try:
        rhr = client.get_rhr_day(date_str)
        vals = (rhr or {}).get("allMetrics", {}).get("metricsMap", {}).get("WELLNESS_RESTING_HEART_RATE", [])
        if vals:
            entry["restingHR"] = vals[0].get("value")
    except Exception:
        pass

    try:
        readiness = client.get_training_readiness(date_str)
        if readiness:
            entry["readinessScore"] = readiness[0].get("score")
    except Exception:
        pass

    return entry


def fetch_activity_detail(client, activity_id):
    """Best-effort per-run detail (splits + weather + cadence) for a matched
    running activity. Returns None if nothing useful could be fetched."""
    detail = {}

    try:
        splits_raw = client.get_activity_splits(activity_id)
        laps = splits_raw.get("lapDTOs", [])
        splits = []
        cadences = []
        for i, lap in enumerate(laps, start=1):
            dist_m = lap.get("distance") or 0
            dur_s = lap.get("duration") or 0
            if dist_m and dur_s:
                pace_sec = dur_s / (dist_m / METERS_PER_MILE)
                pace = f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d}"
            else:
                pace = "—"
            split = {"mi": i, "pace": pace, "hr": lap.get("averageHR")}
            gain_m = lap.get("elevationGain")
            loss_m = lap.get("elevationLoss")
            if gain_m is not None:
                split["elevGain"] = round(gain_m / METERS_PER_FOOT)
            if loss_m is not None:
                split["elevLoss"] = round(loss_m / METERS_PER_FOOT)
            splits.append(split)
            if lap.get("averageRunCadence"):
                cadences.append(lap["averageRunCadence"])
        if splits:
            detail["splits"] = splits
        if cadences:
            detail["cadence"] = round(sum(cadences) / len(cadences))
    except Exception:
        pass

    try:
        weather = client.get_activity_weather(activity_id)
        if weather:
            desc = (weather.get("weatherTypeDTO") or {}).get("desc")
            detail["weather"] = {
                "desc": desc,
                "temp": weather.get("temp"),
                "humidity": weather.get("relativeHumidity"),
            }
    except Exception:
        pass

    return detail or None


def extract_plan(html):
    m = re.search(r"const PLAN = (\[.*?\]);\s*\nconst FITNESS_LOG", html, re.S)
    if not m:
        raise RuntimeError("Could not find PLAN array in index.html")
    import json5

    return json5.loads(m.group(1)), m.span(1)


def extract_fitness_log(html):
    m = re.search(r"const FITNESS_LOG = (\[.*?\]);\s*\n\s*const RACE_DATE", html, re.S)
    if not m:
        raise RuntimeError("Could not find FITNESS_LOG array in index.html")
    import json5

    return json5.loads(m.group(1)), m.span(1)


def week_dates(wk):
    start = datetime.strptime(wk["start"], "%Y-%m-%d").date()
    return [start + timedelta(days=i) for i in range(7)]


def extract_actual_mi(text):
    m = re.search(r"actual:[^0-9]*([\d.]+)\s*mi", text)
    return float(m.group(1)) if m else None


def match_day(day, date_str, activities, detail_fetcher=None):
    """day = [dname, workout, mi, done?, detail?]. Returns updated day (or unchanged)."""
    dname, wo, mi = day[0], day[1], day[2]
    done = day[3] if len(day) > 3 else False
    if done:
        return day

    day_activities = [a for a in activities if a["date"] == date_str]
    if not day_activities:
        return day

    is_rest = wo.strip().lower().startswith("rest")

    if not is_rest:
        runs = [a for a in day_activities if a["type"] in RUNNING_TYPES]
        if not runs:
            return day  # don't fabricate a match
        primary = max(runs, key=lambda a: a["distance_mi"])
        hr = f"{primary['avg_hr'] or '?'}/{primary['max_hr'] or '?'}"
        note = f"(done, actual: {primary['title']} {primary['distance_mi']:.2f}mi — HR {hr})"
        new_day = [dname, f"{wo} {note}", mi, True]
        if detail_fetcher is not None and primary.get("id"):
            detail = detail_fetcher(primary["id"])
            if detail:
                new_day.append(detail)
        return new_day
    else:
        # Rest day: any logged activity counts as cross-training
        parts = []
        hrs_avg, hrs_max = [], []
        for a in day_activities:
            parts.append(f"{a['title']} {a['duration_min']}min")
            if a["avg_hr"]:
                hrs_avg.append(a["avg_hr"])
            if a["max_hr"]:
                hrs_max.append(a["max_hr"])
        joined = " + ".join(parts)
        hr_suffix = f", HR {min(hrs_avg)}-{max(hrs_max)}" if hrs_avg and hrs_max else ""
        note = f"(done — cross-trained: {joined}{hr_suffix})"
        return [dname, f"{wo} {note}", mi, True]


def finalize_and_apply_rule(plan, today):
    """For any fully-past, fully-done week not yet finalized: backfill mi to
    actual, then apply the ahead-of-plan rule to the next week if warranted."""
    summary = []
    for i, wk in enumerate(plan):
        if wk.get("finalized"):
            continue
        end = week_dates(wk)[-1]
        if end >= today:
            continue
        if not all(len(d) > 3 and d[3] for d in wk["days"]):
            continue  # not fully done yet, leave as-is

        planned_total = sum(d[2] for d in wk["days"])
        actual_total = 0.0
        for d in wk["days"]:
            is_rest = d[1].strip().lower().startswith("rest") and "cross-trained" not in d[1]
            if is_rest:
                d[2] = 0
                continue
            actual = extract_actual_mi(d[1])
            if actual is not None:
                d[2] = actual
                actual_total += actual
            else:
                actual_total += d[2]  # no parsed actual, keep planned as fallback

        wk["finalized"] = True
        summary.append(f"Week {wk['w']} finalized: {actual_total:.2f}mi actual vs {planned_total:.2f}mi planned")

        if actual_total > planned_total and i + 1 < len(plan):
            nxt = plan[i + 1]
            if not nxt.get("ahead_of_plan_applied"):
                nxt_planned = sum(d[2] for d in nxt["days"])
                cap = actual_total * 1.10
                if nxt_planned > cap and nxt_planned > 0:
                    factor = cap / nxt_planned
                    for d in nxt["days"]:
                        d[2] = round(d[2] * factor, 1)
                    nxt["note"] = (
                        (nxt.get("note") or "")
                        + f" Ahead-of-plan rule applied: Week {wk['w']} beat plan "
                        + f"({actual_total:.2f}mi vs {planned_total:.2f}mi), so this week is capped at "
                        + f"+10% over that ({cap:.1f}mi)."
                    ).strip()
                    nxt["ahead_of_plan_applied"] = True
                    summary.append(f"Ahead-of-plan rule applied to Week {nxt['w']} (capped at {cap:.1f}mi)")
    return summary


def upsert_fitness_log(log, entry):
    for i, e in enumerate(log):
        if e.get("date") == entry["date"]:
            log[i] = entry
            return "updated"
    log.append(entry)
    return "appended"


def main():
    html = open(HTML_PATH).read()
    plan, plan_span = extract_plan(html)
    today = datetime.now(TZ).date()
    today_str = today.isoformat()

    client = login()
    activities = fetch_activities(client)

    # current week + immediately prior week if still open
    weeks_to_check = []
    for i, wk in enumerate(plan):
        dates = week_dates(wk)
        if dates[0] <= today <= dates[-1]:
            weeks_to_check.append(i)
            if i > 0 and not all(len(d) > 3 and d[3] for d in plan[i - 1]["days"]):
                weeks_to_check.append(i - 1)
            break

    detail_fetcher = lambda activity_id: fetch_activity_detail(client, activity_id)

    changed_days = []
    for i in weeks_to_check:
        wk = plan[i]
        dates = week_dates(wk)
        new_days = []
        for d, date in zip(wk["days"], dates):
            if date > today:
                new_days.append(d)
                continue
            updated = match_day(d, date.isoformat(), activities, detail_fetcher)
            if updated is not d and updated != d:
                changed_days.append(f"Week {wk['w']} {d[0]}")
            new_days.append(updated)
        wk["days"] = new_days

    rule_summary = finalize_and_apply_rule(plan, today)

    new_plan_js = json.dumps(plan)
    html = html[: plan_span[0]] + new_plan_js + html[plan_span[1] :]

    # fitness log: re-extract span on the just-updated html (plan edit may have shifted offsets)
    fitness_log, fitness_span = extract_fitness_log(html)
    fitness_entry = fetch_fitness_metrics(client, today_str)
    fitness_action = upsert_fitness_log(fitness_log, fitness_entry)
    new_fitness_js = json.dumps(fitness_log)
    html = html[: fitness_span[0]] + new_fitness_js + html[fitness_span[1] :]

    now_str = datetime.now(TZ).strftime("%b %-d, %-I:%M %p ET")
    html = re.sub(
        r'(<b id="lastSynced">).*?(</b>)',
        lambda m: m.group(1) + now_str + m.group(2),
        html,
        count=1,
    )

    open(HTML_PATH, "w").write(html)

    print(f"ACTIVITY_COUNT: {len(activities)}")
    print(f"DAYS_UPDATED: {len(changed_days)}")
    for c in changed_days:
        print(f"  - {c}")
    for s in rule_summary:
        print(f"RULE: {s}")
    print(f"FITNESS_LOG: {fitness_action} entry for {today_str} -> {fitness_entry}")
    print(f"LAST_SYNCED: {now_str}")


if __name__ == "__main__":
    main()
