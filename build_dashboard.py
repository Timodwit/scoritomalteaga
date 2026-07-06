"""Regenerate dashboard.html's embedded data.

Run this after scorito_scraper.py to refresh the standings/line-race
dashboard with the latest fetched data. Pulls the official leaderboard and
stat tiles from data/resultaten.json, the per-match line-race series from
compute_scores.py (recomputed fresh each time), and builds the upcoming/live
match predictions view plus per-participant prediction history.
"""
import json
import re
from pathlib import Path

from compute_scores import build_payload as build_computed_payload
from compute_scores import outcome_sign

ROOT = Path(__file__).parent
DASHBOARD_FILE = ROOT / "dashboard.html"

SCORING_RULES = {
    "groupStage": {"winner": 30, "exactScore": 45, "topscorer": {"defenderKeeper": 32, "midfielder": 16, "attacker": 8}},
    "roundOf32": {"winner": 60, "exactScore": 90, "topscorer": {"defenderKeeper": 64, "midfielder": 32, "attacker": 16}},
    "roundOf16": {"winner": 90, "exactScore": 135, "topscorer": {"defenderKeeper": 96, "midfielder": 48, "attacker": 24}},
    "quarterfinal": {"winner": 120, "exactScore": 180, "topscorer": {"defenderKeeper": 128, "midfielder": 64, "attacker": 32}},
    "semifinal": {"winner": 150, "exactScore": 225, "topscorer": {"defenderKeeper": 160, "midfielder": 80, "attacker": 40}},
    "finalAndThird": {"winner": 180, "exactScore": 270, "topscorer": {"defenderKeeper": 192, "midfielder": 96, "attacker": 48}},
    "championPick": 250,
    "groupPositionPerTeam": 25,
}


def build_payload() -> dict:
    with open(ROOT / "data" / "resultaten.json", encoding="utf-8") as f:
        d = json.load(f)
    with open(ROOT / "data" / "voorspellingen.json", encoding="utf-8") as f:
        preds = json.load(f)

    participants = sorted(d["participants"], key=lambda p: p["totalPoints"], reverse=True)
    for i, p in enumerate(participants, 1):
        p["rank"] = i
    participant_by_id = {p["userId"]: p for p in participants}

    computed = build_computed_payload()
    computed_by_user = {s["userId"]: s for s in computed["series"]}

    series = []
    for p in participants:
        c = computed_by_user.get(p["userId"], {"cumulative": []})
        series.append(
            {
                "userId": p["userId"],
                "userName": p["userName"],
                "rank": p["rank"],
                "totalPoints": p["totalPoints"],
                "cumulative": c["cumulative"],
            }
        )

    finished_matches = sum(1 for m in d["matches"] if m["status"] == 2)
    total_matches = len(d["matches"])
    goal_count = sum(len(m.get("goals", [])) for m in d["matches"])

    # "biggest riser" = largest single-match jump in the most recently played match
    last_match_gain = {
        s["userId"]: (s["cumulative"][-1] - s["cumulative"][-2]) if len(s["cumulative"]) > 1 else s["cumulative"][-1]
        for s in computed["series"]
        if s["cumulative"]
    }
    riser_value = max(last_match_gain.values()) if last_match_gain else 0
    risers = [
        computed_by_user[uid]["userName"]
        for uid, gain in last_match_gain.items()
        if gain == riser_value and riser_value > 0
    ]
    last_match = computed["matches"][-1] if computed["matches"] else None

    leader = participants[0]

    # ---- players + topscorer picks, resolved for direct display ----
    player_by_enriched_id = {
        p["enrichedId"]: p for p in d["players"] if p.get("enrichedId") is not None
    }
    topscorer_picks_by_user = {
        t["userId"]: [
            player_by_enriched_id[eid]
            for eid in t["playerIds"]
            if eid in player_by_enriched_id
        ]
        for t in preds["topScorerPredictions"]
    }

    def resolved_picks(uid):
        return [
            {"name": pl["name"], "teamName": pl["teamName"], "category": pl.get("category")}
            for pl in topscorer_picks_by_user.get(uid, [])
        ]

    # ---- predictions lookup: (userId, matchId) -> (home, away) ----
    prediction_by_user_match = {
        (p["userId"], p["matchId"]): (p["homeScorePredicted"], p["awayScorePredicted"])
        for p in preds["predictions"]
    }

    # per-match computed points, for showing "you earned N points" on played
    # matches in the participant detail view (aligned with computed["matches"])
    match_points_by_user_match = {}
    for s in computed["series"]:
        for m, pts in zip(computed["matches"], s["matchPoints"]):
            match_points_by_user_match[(s["userId"], m["matchId"])] = pts

    def topscorer_in_play(uid, home_team_id, away_team_id):
        return [
            {"name": pl["name"], "teamName": pl["teamName"]}
            for pl in topscorer_picks_by_user.get(uid, [])
            if pl.get("teamId") in (home_team_id, away_team_id)
        ]

    all_matches_sorted = sorted(d["matches"], key=lambda m: m["matchDate"])

    # ---- upcoming/live matches with everyone's predictions ----
    upcoming_matches = []
    for m in all_matches_sorted:
        if m["status"] == 2:
            continue
        if not m.get("homeTeamFull") and not m["homeTeam"]:
            continue
        if m["homeTeamId"] is None or m["awayTeamId"] is None:
            continue
        is_live = m["status"] == 1
        actual = {"home": m["homeScore"], "away": m["awayScore"]} if is_live else None
        predictions = []
        for p in participants:
            uid = p["userId"]
            pred = prediction_by_user_match.get((uid, m["matchId"]))
            result_class = None
            if is_live and pred:
                if (pred[0], pred[1]) == (m["homeScore"], m["awayScore"]):
                    result_class = "correct"
                elif outcome_sign(pred[0], pred[1]) == outcome_sign(m["homeScore"], m["awayScore"]):
                    result_class = "correct"
                else:
                    result_class = "wrong"
            predictions.append(
                {
                    "userId": uid,
                    "userName": p["userName"],
                    "predicted": {"home": pred[0], "away": pred[1]} if pred else None,
                    "resultClass": result_class,
                    "topscorerInPlay": topscorer_in_play(uid, m["homeTeamId"], m["awayTeamId"]),
                }
            )
        upcoming_matches.append(
            {
                "matchId": m["matchId"],
                "matchDate": m["matchDate"],
                "roundName": m["roundName"],
                "homeTeam": m.get("homeTeamFull") or m["homeTeam"],
                "awayTeam": m.get("awayTeamFull") or m["awayTeam"],
                "status": m["status"],
                "actual": actual,
                "predictions": predictions,
            }
        )

    live_match = next((m for m in upcoming_matches if m["status"] == 1), None)

    # ---- per-participant full prediction history (past + upcoming) ----
    participant_details = {}
    for p in participants:
        uid = p["userId"]
        match_rows = []
        for m in all_matches_sorted:
            home_name = m.get("homeTeamFull") or m["homeTeam"]
            away_name = m.get("awayTeamFull") or m["awayTeam"]
            if not home_name or not away_name:
                continue
            pred = prediction_by_user_match.get((uid, m["matchId"]))
            row = {
                "matchId": m["matchId"],
                "matchDate": m["matchDate"],
                "roundName": m["roundName"],
                "homeTeam": home_name,
                "awayTeam": away_name,
                "status": m["status"],
                "predicted": {"home": pred[0], "away": pred[1]} if pred else None,
                "actual": (
                    {"home": m["homeScore"], "away": m["awayScore"]}
                    if m["status"] == 2
                    else None
                ),
                "points": (
                    match_points_by_user_match.get((uid, m["matchId"]))
                    if m["status"] == 2
                    else None
                ),
                "topscorerInPlay": topscorer_in_play(uid, m["homeTeamId"], m["awayTeamId"])
                if m["homeTeamId"] is not None and m["awayTeamId"] is not None
                else [],
            }
            match_rows.append(row)

        participant_details[uid] = {
            "userId": uid,
            "userName": p["userName"],
            "rank": p["rank"],
            "totalPoints": p["totalPoints"],
            "topscorerPicks": resolved_picks(uid),
            "matches": match_rows,
        }

    return {
        "pool": d["pool"],
        "matches": computed["matches"],
        "series": series,
        "stats": {
            "leaderName": leader["userName"],
            "leaderPoints": leader["totalPoints"],
            "riserNames": risers,
            "riserPoints": round(riser_value, 1),
            "riserMatch": (
                f"{last_match['homeTeam']} - {last_match['awayTeam']}" if last_match else ""
            ),
            "finishedMatches": finished_matches,
            "totalMatches": total_matches,
            "goalCount": goal_count,
        },
        "scoringRules": SCORING_RULES,
        "upcomingMatches": upcoming_matches,
        "liveMatchId": live_match["matchId"] if live_match else None,
        "participantDetails": participant_details,
    }


def main() -> None:
    payload = build_payload()
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)

    html = DASHBOARD_FILE.read_text(encoding="utf-8")
    pattern = re.compile(
        r'(<script id="dashboard-data" type="application/json">\n).*?(\n</script>)',
        re.S,
    )
    new_html, count = pattern.subn(lambda m: m.group(1) + payload_json + m.group(2), html)
    if count != 1:
        raise RuntimeError(
            "Could not find the dashboard-data script block in dashboard.html "
            "-- did the file structure change?"
        )
    DASHBOARD_FILE.write_text(new_html, encoding="utf-8")
    print(
        f"Updated {DASHBOARD_FILE} with fresh data "
        f"({len(payload['series'])} participants, {len(payload['matches'])} matches, "
        f"{len(payload['upcomingMatches'])} upcoming)."
    )


if __name__ == "__main__":
    main()
