"""Compute per-match ("per game") point totals from raw predictions + results.

Scorito only exposes official point totals per *round* (via scoreblock), not
per match. This script recomputes match-only points (winner/exact-score) from
the pool's own scoring rules and calibrates each round's match-by-match shape
so it sums exactly to Scorito's official per-round total. Three further
bonuses are computed separately and added directly on top (verified
empirically that they are NOT part of the official per-round totals -- a
round's match-only total already lands close to the official figure with
nothing else added):

- Topscorer-goal bonus: added at the match the goal was scored in.
- Champion-pick bonus (250 pts): added once the final is played.
- Group-position bonus (25 pts/team): not a separate prediction -- it's
  derived by simulating each participant's predicted group table from their
  own group-match predictions (standard points/goal-difference/goals-for
  tiebreakers) and comparing it to the real final standings. Added once the
  group stage ends.

Whatever small gap remains after all of that (mostly tiebreaker-rule
approximation noise) is banked as a disclosed starting balance, so the
line's endpoint still lands exactly on the official total.

Writes data/computed_scores.json.
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"

ROUND_NAMES = {
    1: "Groepsfase",
    2: "Groepsfase",
    3: "Groepsfase",
    4: "Laatste 32",
    5: "1/8e finale",
    6: "Kwartfinale",
    7: "Halve finale",
    8: "Troostfinale",
    9: "Finale",
}

# tier multiplier per EventRoundOrder, derived from the pool's point table:
# every value scales exactly 1x/2x/3x/4x/5x/6x from the group-stage base.
TIER_MULT = {1: 1, 2: 1, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 6}

BASE_WINNER = 30
BASE_EXACT = 45
BASE_TOPSCORER = {"defenderKeeper": 32, "midfielder": 16, "attacker": 8}

CHAMPION_BONUS = 250
GROUP_POSITION_BONUS = 25


def outcome_sign(home: int, away: int) -> int:
    return (home > away) - (home < away)


def apportion_integers(shares: list[float], total: int) -> list[int]:
    """Distribute an integer total across `shares` proportionally, using the
    largest-remainder method, so every match gets a whole number of points
    and the group still sums to exactly `total` -- no fractional points from
    proportional scaling (every rule in the point table is a whole number,
    so the output should be too).
    """
    total = round(total)
    n = len(shares)
    if n == 0:
        return []
    total_share = sum(shares)
    if total_share <= 0:
        base, remainder = divmod(total, n)
        return [base + (1 if i < remainder else 0) for i in range(n)]

    exact = [total * s / total_share for s in shares]
    floors = [int(x) for x in exact]
    remainder = total - sum(floors)
    order = sorted(range(n), key=lambda i: exact[i] - floors[i], reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return floors


def compute_group_position_bonus(results: dict, prediction_by_user_match: dict, participants: list[dict]):
    """25 pts per team a participant placed correctly in the final group table.

    Not a separate prediction: it's derived by simulating each participant's
    predicted group table from their own group-match predictions (using
    standard points/goal-difference/goals-for tiebreakers) and comparing the
    resulting position per team against the real final standings.
    """
    standings = results.get("groupStandings", [])
    bonus = {p["userId"]: 0.0 for p in participants}
    if not standings:
        return bonus

    team_to_group = {s["teamId"]: s["groupNumber"] for s in standings}
    team_to_actual_rank = {s["teamId"]: s["actualRank"] for s in standings}
    groups_teams: dict[int, set] = {}
    for s in standings:
        groups_teams.setdefault(s["groupNumber"], set()).add(s["teamId"])

    group_matches: dict[int, list[dict]] = {}
    for m in results["matches"]:
        if m["roundOrder"] not in (1, 2, 3):
            continue
        group_num = team_to_group.get(m["homeTeamId"])
        if group_num is not None and team_to_group.get(m["awayTeamId"]) == group_num:
            group_matches.setdefault(group_num, []).append(m)

    for p in participants:
        uid = p["userId"]
        for group_num, teams in groups_teams.items():
            table = {tid: {"points": 0, "gf": 0, "ga": 0} for tid in teams}
            for m in group_matches.get(group_num, []):
                pred = prediction_by_user_match.get((uid, m["matchId"]))
                if not pred:
                    continue
                home_id, away_id = m["homeTeamId"], m["awayTeamId"]
                table[home_id]["gf"] += pred[0]
                table[home_id]["ga"] += pred[1]
                table[away_id]["gf"] += pred[1]
                table[away_id]["ga"] += pred[0]
                if pred[0] > pred[1]:
                    table[home_id]["points"] += 3
                elif pred[0] < pred[1]:
                    table[away_id]["points"] += 3
                else:
                    table[home_id]["points"] += 1
                    table[away_id]["points"] += 1

            ranked = sorted(
                teams,
                key=lambda tid: (
                    -table[tid]["points"],
                    -(table[tid]["gf"] - table[tid]["ga"]),
                    -table[tid]["gf"],
                ),
            )
            for predicted_rank, team_id in enumerate(ranked, 1):
                if team_to_actual_rank.get(team_id) == predicted_rank:
                    bonus[uid] += GROUP_POSITION_BONUS

    return bonus


def build_payload() -> dict:
    with open(DATA_DIR / "resultaten.json", encoding="utf-8") as f:
        results = json.load(f)
    with open(DATA_DIR / "voorspellingen.json", encoding="utf-8") as f:
        preds = json.load(f)

    participants = results["participants"]
    matches = [m for m in results["matches"] if m["status"] == 2]
    matches.sort(key=lambda m: m["matchDate"])

    player_category = {p["playerId"]: p.get("category") for p in results["players"]}
    # Topscorer predictions identify players by "TeamPlayerEnrichedId", a
    # completely different id space from the "PlayerId" used in goal events
    # -- translate goal scorers to that id space before matching them up.
    player_enriched_id = {p["playerId"]: p.get("enrichedId") for p in results["players"]}

    prediction_by_user_match = {
        (p["userId"], p["matchId"]): (p["homeScorePredicted"], p["awayScorePredicted"])
        for p in preds["predictions"]
    }
    topscorer_picks = {
        t["userId"]: set(t["playerIds"]) for t in preds["topScorerPredictions"]
    }
    champion_picks = {
        c["userId"]: c["teamId"] for c in preds.get("championPredictions", [])
    }

    official_round_points = {
        p["userId"]: {} for p in participants
    }
    for s in results["roundScores"]:
        official_round_points[s["userId"]][s["roundName"]] = s["points"]

    # 1. raw match-only points (winner/exact) per (userId, matchId) -- this is
    # what gets calibrated to Scorito's official per-round totals. Topscorer
    # and champion bonuses are tracked separately: evidence shows official
    # round totals are match-prediction-only (a round's match-only total
    # already lands within ~5% of the official figure with nothing extra
    # added), so folding topscorer bonus into the calibrated pool would just
    # get diluted away instead of actually counting.
    match_only_points: dict[tuple[int, int], float] = {}
    topscorer_points: dict[tuple[int, int], float] = {}
    for m in matches:
        tier = TIER_MULT[m["roundOrder"]]
        actual_outcome = outcome_sign(m["homeScore"], m["awayScore"])

        for p in participants:
            uid = p["userId"]
            pts = 0.0
            pred = prediction_by_user_match.get((uid, m["matchId"]))
            if pred:
                if pred == (m["homeScore"], m["awayScore"]):
                    pts = BASE_EXACT * tier
                elif outcome_sign(pred[0], pred[1]) == actual_outcome:
                    pts = BASE_WINNER * tier
            match_only_points[(uid, m["matchId"])] = pts

            ts_pts = 0.0
            for goal in m.get("goals", []):
                enriched_id = player_enriched_id.get(goal["playerId"])
                if enriched_id is not None and enriched_id in topscorer_picks.get(uid, ()):
                    cat = player_category.get(goal["playerId"])
                    if cat:
                        ts_pts += BASE_TOPSCORER[cat] * tier
            topscorer_points[(uid, m["matchId"])] = ts_pts

    # champion bonus resolves once the final is played; added directly to
    # that match, same as topscorer bonus -- a real addition, not calibrated.
    final_match = next((m for m in matches if m["roundOrder"] == 9), None)
    champion_bonus_by_user: dict[int, float] = {p["userId"]: 0.0 for p in participants}
    if final_match and final_match["roundWinnerType"] in (1, 2):
        champion_team_id = (
            final_match["homeTeamId"]
            if final_match["roundWinnerType"] == 1
            else final_match["awayTeamId"]
        )
        for p in participants:
            uid = p["userId"]
            if champion_picks.get(uid) == champion_team_id:
                champion_bonus_by_user[uid] = CHAMPION_BONUS

    # group-position bonus resolves once the group stage ends; added directly
    # to the last group-stage match, same treatment as the other bonuses.
    group_bonus_by_user = compute_group_position_bonus(results, prediction_by_user_match, participants)
    group_matches_played = [m for m in matches if m["roundOrder"] in (1, 2, 3)]
    last_group_match = max(group_matches_played, key=lambda m: m["matchDate"]) if group_matches_played else None

    # 2. calibrate each round's match-only shape to sum to the official round total
    matches_by_round: dict[str, list[dict]] = {}
    for m in matches:
        matches_by_round.setdefault(ROUND_NAMES[m["roundOrder"]], []).append(m)

    calibrated_points: dict[tuple[int, int], int] = {}
    for p in participants:
        uid = p["userId"]
        for round_name, round_matches in matches_by_round.items():
            official_total = official_round_points[uid].get(round_name, 0)
            shares = [match_only_points[(uid, m["matchId"])] for m in round_matches]
            allocation = apportion_integers(shares, official_total)
            for m, pts in zip(round_matches, allocation):
                calibrated_points[(uid, m["matchId"])] = pts

    # 3. whatever's still unexplained after match points + topscorer + champion
    # + group-position bonus -- should now be small (rule-approximation noise,
    # e.g. tiebreaker edge cases in the simulated group table) -- is banked as
    # a disclosed starting balance so the line's endpoint still lands exactly
    # on the official total.
    other_bonus_by_user: dict[int, float] = {}
    for p in participants:
        uid = p["userId"]
        explained = (
            sum(calibrated_points[(uid, m["matchId"])] for m in matches)
            + sum(topscorer_points[(uid, m["matchId"])] for m in matches)
            + champion_bonus_by_user[uid]
            + group_bonus_by_user[uid]
        )
        other_bonus_by_user[uid] = p["totalPoints"] - explained

    # 4. cumulative AND per-match (non-cumulative) series per participant, in
    # chronological order. matchPoints is what a single match actually
    # contributed (match points + any bonus resolved on it) -- useful for a
    # "here's what you earned in this game" view, separate from the running
    # total the line race needs.
    series = []
    for p in sorted(participants, key=lambda p: p["totalPoints"], reverse=True):
        uid = p["userId"]
        cumulative = []
        match_points = []
        running = other_bonus_by_user[uid]
        for m in matches:
            this_match = calibrated_points[(uid, m["matchId"])] + topscorer_points[(uid, m["matchId"])]
            if m["matchId"] == (final_match["matchId"] if final_match else None):
                this_match += champion_bonus_by_user[uid]
            if m["matchId"] == (last_group_match["matchId"] if last_group_match else None):
                this_match += group_bonus_by_user[uid]
            running += this_match
            match_points.append(int(round(this_match)))
            cumulative.append(int(round(running)))
        series.append(
            {
                "userId": uid,
                "userName": p["userName"],
                "totalPoints": p["totalPoints"],
                "groupPositionBonus": int(round(group_bonus_by_user[uid])),
                "otherBonus": int(round(other_bonus_by_user[uid])),
                "cumulative": cumulative,
                "matchPoints": match_points,
            }
        )
    for i, s in enumerate(series, 1):
        s["rank"] = i

    match_index = [
        {
            "matchId": m["matchId"],
            "matchDate": m["matchDate"],
            "roundName": ROUND_NAMES[m["roundOrder"]],
            "homeTeam": m.get("homeTeamFull") or m["homeTeam"],
            "awayTeam": m.get("awayTeamFull") or m["awayTeam"],
            "homeScore": m["homeScore"],
            "awayScore": m["awayScore"],
        }
        for m in matches
    ]

    return {
        "pool": results["pool"],
        "matches": match_index,
        "series": series,
        "notes": (
            "Cumulative points = calibrated match winner/exact-score points "
            "(anchored to Scorito's official per-round totals) + real "
            "topscorer-goal, champion-pick, and simulated group-position "
            "bonuses, plus a small disclosed starting balance covering "
            "remaining rule-approximation noise."
        ),
    }


def main() -> None:
    payload = build_payload()
    out_path = DATA_DIR / "computed_scores.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(
        f"Wrote {out_path} ({len(payload['matches'])} matches, "
        f"{len(payload['series'])} participants)"
    )


if __name__ == "__main__":
    main()
