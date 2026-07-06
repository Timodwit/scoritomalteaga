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
from compute_scores import outcome_sign, TIER_MULT, BASE_WINNER, BASE_EXACT, BASE_TOPSCORER

# Canonical round order for grouping the match list, most recent first (so
# the live/upcoming action surfaces near the top instead of being buried
# under 90+ already-played group-stage matches).
ROUND_ORDER = [
    "Finale", "Troostfinale", "Halve finale", "Kwartfinale",
    "1/8e finale", "Laatste 32", "Ronde 3", "Ronde 2", "Ronde 1",
]

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

    # "biggest riser" = largest gain across the most recently active ROUND
    # (all matches sharing the round of the last-played match), not just a
    # single game -- a round can span many matches (e.g. 24 in the group
    # stage), so judging by one match alone was misleading.
    last_match = computed["matches"][-1] if computed["matches"] else None
    current_round_name = last_match["roundName"] if last_match else None
    round_match_indices = [
        i for i, m in enumerate(computed["matches"]) if m["roundName"] == current_round_name
    ]
    round_gain = {
        s["userId"]: sum(s["matchPoints"][i] for i in round_match_indices)
        for s in computed["series"]
    }
    riser_value = max(round_gain.values()) if round_gain else 0
    risers = [
        computed_by_user[uid]["userName"]
        for uid, gain in round_gain.items()
        if gain == riser_value and riser_value > 0
    ]

    # ---- per-round winners: who scored the most points in EACH round
    # that's been played so far (not just the most recent one) -- in
    # forward chronological order, i.e. the tournament's own story.
    round_order_seen = []
    round_indices = {}
    for i, m in enumerate(computed["matches"]):
        if m["roundName"] not in round_indices:
            round_order_seen.append(m["roundName"])
            round_indices[m["roundName"]] = []
        round_indices[m["roundName"]].append(i)

    round_winners = []
    for round_name in round_order_seen:
        indices = round_indices[round_name]
        gains = {
            s["userId"]: sum(s["matchPoints"][i] for i in indices) for s in computed["series"]
        }
        best = max(gains.values()) if gains else 0
        winners = [
            computed_by_user[uid]["userName"]
            for uid, gain in gains.items()
            if gain == best and best > 0
        ]
        round_winners.append({"roundName": round_name, "winnerNames": winners, "points": best})

    leader = participants[0]

    # ---- players + topscorer picks, resolved for direct display ----
    # Topscorer picks change every round, so everything here is keyed by
    # (userId, tier) rather than just userId -- tier is the same 1-6 scale
    # match points use (group=1 ... final/3rd=6).
    player_by_enriched_id = {
        p["enrichedId"]: p for p in d["players"] if p.get("enrichedId") is not None
    }
    player_category = {p["playerId"]: p.get("category") for p in d["players"]}
    player_enriched_id = {p["playerId"]: p.get("enrichedId") for p in d["players"]}

    topscorer_picks_by_user_tier = {}
    for t in preds["topScorerPredictions"]:
        key = (t["userId"], t["tier"])
        topscorer_picks_by_user_tier[key] = {
            "roundName": t["roundName"],
            "enrichedIds": set(t["playerIds"]),
            "players": [
                player_by_enriched_id[eid] for eid in t["playerIds"] if eid in player_by_enriched_id
            ],
        }

    def resolved_picks_by_tier(uid):
        entries = []
        for tier in range(1, 7):
            info = topscorer_picks_by_user_tier.get((uid, tier))
            if not info:
                continue
            entries.append(
                {
                    "tier": tier,
                    "roundName": info["roundName"],
                    "picks": [
                        {"name": pl["name"], "teamName": pl["teamName"], "category": pl.get("category")}
                        for pl in info["players"]
                    ],
                }
            )
        return entries

    def topscorer_in_play(uid, tier, home_team_id, away_team_id):
        info = topscorer_picks_by_user_tier.get((uid, tier))
        if not info:
            return []
        return [
            {"name": pl["name"], "teamName": pl["teamName"]}
            for pl in info["players"]
            if pl.get("teamId") in (home_team_id, away_team_id)
        ]

    # ---- predictions lookup: (userId, matchId) -> (home, away) ----
    prediction_by_user_match = {
        (p["userId"], p["matchId"]): (p["homeScorePredicted"], p["awayScorePredicted"])
        for p in preds["predictions"]
    }

    # per-match computed points + exact/winner/miss labels, for the "you
    # earned N points" view on played matches (aligned with computed["matches"])
    match_points_by_user_match = {}
    result_type_by_user_match = {}
    for s in computed["series"]:
        for m, pts, rtype in zip(computed["matches"], s["matchPoints"], s["resultTypes"]):
            match_points_by_user_match[(s["userId"], m["matchId"])] = pts
            result_type_by_user_match[(s["userId"], m["matchId"])] = rtype

    def points_for_match(uid, m, tier):
        """Points a prediction earns for this match -- final if the match has
        finished, or "right now" if it's still live (based on the current
        score and goals scored so far). Same formula either way; only the
        goals/score inputs change as the match progresses."""
        pred = prediction_by_user_match.get((uid, m["matchId"]))
        match_pts = 0
        result_class = None
        if pred:
            if (pred[0], pred[1]) == (m["homeScore"], m["awayScore"]):
                match_pts = BASE_EXACT * tier
                result_class = "exact"
            elif outcome_sign(pred[0], pred[1]) == outcome_sign(m["homeScore"], m["awayScore"]):
                match_pts = BASE_WINNER * tier
                result_class = "winner"
            else:
                result_class = "miss"

        ts_points = 0
        ts_info = topscorer_picks_by_user_tier.get((uid, tier))
        if ts_info:
            for goal in m.get("goals", []):
                eid = player_enriched_id.get(goal["playerId"])
                if eid is not None and eid in ts_info["enrichedIds"]:
                    cat = player_category.get(goal["playerId"])
                    if cat:
                        ts_points += BASE_TOPSCORER[cat] * tier

        return match_pts, ts_points, result_class

    all_matches_sorted = sorted(d["matches"], key=lambda m: m["matchDate"])

    # ---- every match (past, live, upcoming) with everyone's predictions,
    # grouped by round so results and upcoming games can both be browsed
    # round by round instead of one long chronological list.
    matches_by_round_dict = {}
    for m in all_matches_sorted:
        if not m.get("homeTeamFull") and not m["homeTeam"]:
            continue
        if m["homeTeamId"] is None or m["awayTeamId"] is None:
            continue
        is_live = m["status"] == 1
        is_finished = m["status"] == 2
        tier = TIER_MULT[m["roundOrder"]]
        actual = (
            {"home": m["homeScore"], "away": m["awayScore"]} if (is_live or is_finished) else None
        )
        predictions = []
        for p in participants:
            uid = p["userId"]
            pred = prediction_by_user_match.get((uid, m["matchId"]))
            result_class = None
            match_pts = ts_pts = None
            if is_live or is_finished:
                match_pts, ts_pts, result_class = points_for_match(uid, m, tier)
            predictions.append(
                {
                    "userId": uid,
                    "userName": p["userName"],
                    "predicted": {"home": pred[0], "away": pred[1]} if pred else None,
                    "resultClass": result_class,
                    "matchPoints": match_pts,
                    "topscorerPoints": ts_pts,
                    "totalPoints": (match_pts + ts_pts) if (is_live or is_finished) else None,
                    "topscorerInPlay": topscorer_in_play(uid, tier, m["homeTeamId"], m["awayTeamId"]),
                }
            )
        entry = {
            "matchId": m["matchId"],
            "matchDate": m["matchDate"],
            "roundName": m["roundName"],
            "homeTeam": m.get("homeTeamFull") or m["homeTeam"],
            "awayTeam": m.get("awayTeamFull") or m["awayTeam"],
            "status": m["status"],
            "actual": actual,
            "predictions": predictions,
        }
        matches_by_round_dict.setdefault(m["roundName"], []).append(entry)

    matches_by_round = [
        {"roundName": name, "matches": matches_by_round_dict[name]}
        for name in ROUND_ORDER
        if name in matches_by_round_dict
    ]

    live_match = next(
        (m for round_group in matches_by_round for m in round_group["matches"] if m["status"] == 1),
        None,
    )

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
            tier = TIER_MULT[m["roundOrder"]]
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
                "resultType": (
                    result_type_by_user_match.get((uid, m["matchId"]))
                    if m["status"] == 2
                    else None
                ),
                "topscorerInPlay": topscorer_in_play(uid, tier, m["homeTeamId"], m["awayTeamId"])
                if m["homeTeamId"] is not None and m["awayTeamId"] is not None
                else [],
            }
            match_rows.append(row)

        participant_details[uid] = {
            "userId": uid,
            "userName": p["userName"],
            "rank": p["rank"],
            "totalPoints": p["totalPoints"],
            "topscorerPicksByRound": resolved_picks_by_tier(uid),
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
            "riserRound": current_round_name or "",
            "finishedMatches": finished_matches,
            "totalMatches": total_matches,
            "goalCount": goal_count,
        },
        "scoringRules": SCORING_RULES,
        "matchesByRound": matches_by_round,
        "roundWinners": round_winners,
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
    total_view_matches = sum(len(g["matches"]) for g in payload["matchesByRound"])
    print(
        f"Updated {DASHBOARD_FILE} with fresh data "
        f"({len(payload['series'])} participants, {len(payload['matches'])} matches, "
        f"{total_view_matches} in match browser across {len(payload['matchesByRound'])} rounds)."
    )


if __name__ == "__main__":
    main()
