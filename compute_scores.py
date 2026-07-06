"""Compute per-match ("per game") point totals from raw predictions + results.

Every point value here is computed directly from the pool's own scoring
rules -- no calibration against Scorito's official per-round totals is
needed. That was verified empirically: recomputing match-only points
(winner/exact-score, scaled by the round's tier multiplier) from raw
predictions lands on Scorito's own per-round score EXACTLY, for every round
tested, once two data bugs were fixed:

- The three group-stage matchdays ("Ronde 1"/"Ronde 2"/"Ronde 3" in
  Scorito's own UI) are independent rounds with independent scores, NOT one
  cumulative "Groepsfase" total -- treating Ronde 3's own total as if it
  covered all 72 group matches caused a ~3x overcount.
- Goal-scorer data must be fetched per matchday round id, or matchdays 1-2's
  goals go silently missing and their topscorer bonuses are never counted.

Three further bonuses are computed and added at the match where they
resolve:

- Topscorer-goal bonus: added at the match the goal was scored in.
- Champion-pick bonus (250 pts): added once the final is played.
- Group-position bonus (25 pts/team): not a separate prediction -- it's
  derived by simulating each participant's predicted group table from their
  own group-match predictions (standard points/goal-difference/goals-for
  tiebreakers) and comparing it to the real final standings. Added once each
  group's own matches finish (confirmed this bonus is folded into Ronde 3's
  official total, exactly matching direct computation with no gap).

Run standalone (`python compute_scores.py`) to compute and print QA warnings
for every poule in pools.json, writing each one's payload to
data/pools/{slug}-computed.json for inspection. build_dashboard.py is the
normal caller and does not use these files -- it calls build_payload()
directly per poule.
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"

ROUND_NAMES = {
    1: "Ronde 1",
    2: "Ronde 2",
    3: "Ronde 3",
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


def _matches_by_round(matches: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for m in matches:
        grouped.setdefault(ROUND_NAMES[m["roundOrder"]], []).append(m)
    return grouped


def compute_group_position_bonus(results: dict, prediction_by_user_match: dict, participants: list[dict]):
    """25 pts per team a participant placed correctly in the final group table.

    Not a separate prediction: it's derived by simulating each participant's
    predicted group table from their own group-match predictions (using
    standard points/goal-difference/goals-for tiebreakers) and comparing the
    resulting position per team against the real final standings.

    Each of the 12 groups finalizes at its own moment -- as soon as that
    specific group's last match finishes, not when the whole group stage
    across all groups wraps up. So this returns the bonus PER GROUP, plus
    which match resolves each group, rather than one lump sum.

    Returns (bonus_by_user_group, final_match_by_group):
      bonus_by_user_group: {(userId, groupNumber): points}
      final_match_by_group: {groupNumber: matchId} -- only present once ALL
        of that group's matches have been played.
    """
    standings = results.get("groupStandings", [])
    bonus: dict[tuple[int, int], int] = {}
    final_match_by_group: dict[int, int] = {}
    if not standings:
        return bonus, final_match_by_group

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

    group_is_final: dict[int, bool] = {}
    for group_num, group_ms in group_matches.items():
        finished = [m for m in group_ms if m["status"] == 2]
        group_is_final[group_num] = len(finished) == len(group_ms) and len(group_ms) > 0
        if group_is_final[group_num]:
            final_match_by_group[group_num] = max(finished, key=lambda m: m["matchDate"])["matchId"]

    for p in participants:
        uid = p["userId"]
        for group_num, teams in groups_teams.items():
            if not group_is_final.get(group_num):
                continue  # this group's standing isn't set in stone yet

            group_matches_list = group_matches.get(group_num, [])
            pred_by_match = {
                m["matchId"]: prediction_by_user_match.get((uid, m["matchId"]))
                for m in group_matches_list
            }
            group_bonus = 0
            # A participant with zero predictions for this group has no real
            # signal at all -- every team ties at 0pts/0-0, so the "ranking"
            # is just whatever arbitrary order the tiebreaker falls back to,
            # not a real guess. Scoring that would occasionally hand out a
            # bonus purely by chance to someone who predicted nothing.
            if any(pred_by_match.values()):
                ranked = _rank_teams_by_predictions(teams, group_matches_list, pred_by_match)
                for predicted_rank, team_id in enumerate(ranked, 1):
                    if team_to_actual_rank.get(team_id) == predicted_rank:
                        group_bonus += GROUP_POSITION_BONUS
            bonus[(uid, group_num)] = group_bonus

    return bonus, final_match_by_group


def _table_from_predictions(teams: set, matches: list[dict], pred_by_match: dict) -> dict:
    table = {tid: {"points": 0, "gf": 0, "ga": 0} for tid in teams}
    for m in matches:
        if m["homeTeamId"] not in table or m["awayTeamId"] not in table:
            continue
        pred = pred_by_match.get(m["matchId"])
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
    return table


def _rank_teams_by_predictions(teams: set, matches: list[dict], pred_by_match: dict) -> list:
    """Rank a group's teams from one participant's own predictions, using
    FIFA's own World Cup tiebreaker order: points, goal difference, goals
    scored, head-to-head. Verified against Scorito's own displayed predicted
    group table (via the Gamecenter UI) that this order -- not head-to-head
    first, which is UEFA's rule, not FIFA's -- is what Scorito actually uses.
    Teams still tied after all four criteria (only possible if every match
    between them was a predicted draw) can't be resolved from prediction
    data alone -- FIFA's own next tiebreakers are disciplinary "fair play"
    points and a drawn lot, neither of which exists in a predicted table.
    """
    table = _table_from_predictions(teams, matches, pred_by_match)

    def resolve_tied_block(tied_teams: list) -> list:
        if len(tied_teams) == 1:
            return tied_teams
        by_gd_gf = sorted(
            tied_teams,
            key=lambda tid: (-(table[tid]["gf"] - table[tid]["ga"]), -table[tid]["gf"]),
        )
        ordered = []
        i = 0
        while i < len(by_gd_gf):
            j = i + 1
            while j < len(by_gd_gf) and (
                table[by_gd_gf[j]]["gf"] - table[by_gd_gf[j]]["ga"],
                table[by_gd_gf[j]]["gf"],
            ) == (
                table[by_gd_gf[i]]["gf"] - table[by_gd_gf[i]]["ga"],
                table[by_gd_gf[i]]["gf"],
            ):
                j += 1
            still_tied = by_gd_gf[i:j]
            if len(still_tied) > 1:
                h2h_table = _table_from_predictions(set(still_tied), matches, pred_by_match)
                still_tied = sorted(still_tied, key=lambda tid: -h2h_table[tid]["points"])
            ordered.extend(still_tied)
            i = j
        return ordered

    by_points = sorted(teams, key=lambda tid: -table[tid]["points"])
    ranked = []
    i = 0
    while i < len(by_points):
        j = i + 1
        while j < len(by_points) and table[by_points[j]]["points"] == table[by_points[i]]["points"]:
            j += 1
        ranked.extend(resolve_tied_block(by_points[i:j]))
        i = j
    return ranked


def build_payload(participants: list[dict], results: dict, preds: dict) -> dict:
    """Compute one poule's scoring payload.

    `participants` is that poule's own roster; `results` (shared tournament
    data: matches/players/groupStandings/roundScores) and `preds`
    (predictions/topscorer/champion picks, for every participant across
    every poule) are identical for every poule of the same tournament, so
    the caller loads them once and passes them in rather than this function
    reading fixed files itself.
    """
    # Status 1 (in progress) counts too: a live match's CURRENT score earns
    # provisional points that shift with every goal -- that's the whole point
    # of the live-update loop. Group-position bonuses still require the whole
    # group finished (compute_group_position_bonus checks status == 2 itself),
    # and the champion bonus requires roundWinnerType, set only at full time.
    matches = [m for m in results["matches"] if m["status"] in (1, 2)]
    matches.sort(key=lambda m: m["matchDate"])
    live_round_names = {m["roundName"] for m in matches if m["status"] == 1}

    player_category = {p["playerId"]: p.get("category") for p in results["players"]}
    # Topscorer predictions identify players by "TeamPlayerEnrichedId", a
    # completely different id space from the "PlayerId" used in goal events
    # -- translate goal scorers to that id space before matching them up.
    player_enriched_id = {p["playerId"]: p.get("enrichedId") for p in results["players"]}

    prediction_by_user_match = {
        (p["userId"], p["matchId"]): (p["homeScorePredicted"], p["awayScorePredicted"])
        for p in preds["predictions"]
    }
    # Topscorer picks can change every round -- keyed by (userId, tier), not
    # just userId. `tier` here already matches TIER_MULT's 1-6 scale.
    topscorer_picks_by_tier = {
        (t["userId"], t["tier"]): set(t["playerIds"]) for t in preds["topScorerPredictions"]
    }
    champion_picks = {
        c["userId"]: c["teamId"] for c in preds.get("championPredictions", [])
    }

    # roundScores covers every unique participant across ALL poules (shared
    # data), not just this poule's own roster, so build this from whichever
    # userIds actually appear rather than pre-seeding just this poule's own.
    official_round_points: dict[int, dict[str, int]] = {}
    for s in results["roundScores"]:
        official_round_points.setdefault(s["userId"], {})[s["roundName"]] = s["points"]

    # 1. raw match-only points (winner/exact), computed directly from the
    # pool's own scoring rules -- no calibration needed. Verified empirically
    # against Scorito's own per-round totals (via the Gamecenter UI's
    # per-participant match breakdown) that this formula reproduces the
    # official score EXACTLY, once goals are fetched per matchday round id.
    match_only_points: dict[tuple[int, int], int] = {}
    topscorer_points: dict[tuple[int, int], int] = {}
    result_type: dict[tuple[int, int], str] = {}
    for m in matches:
        tier = TIER_MULT[m["roundOrder"]]
        actual_outcome = outcome_sign(m["homeScore"], m["awayScore"])

        for p in participants:
            uid = p["userId"]
            pts = 0
            pred = prediction_by_user_match.get((uid, m["matchId"]))
            if pred:
                if pred == (m["homeScore"], m["awayScore"]):
                    pts = BASE_EXACT * tier
                    result_type[(uid, m["matchId"])] = "exact"
                elif outcome_sign(pred[0], pred[1]) == actual_outcome:
                    pts = BASE_WINNER * tier
                    result_type[(uid, m["matchId"])] = "winner"
                else:
                    result_type[(uid, m["matchId"])] = "miss"
            match_only_points[(uid, m["matchId"])] = pts

            ts_pts = 0
            picks = topscorer_picks_by_tier.get((uid, tier), ())
            for goal in m.get("goals", []):
                enriched_id = player_enriched_id.get(goal["playerId"])
                if enriched_id is not None and enriched_id in picks:
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

    # group-position bonus: each of the 12 groups finalizes independently, the
    # moment THAT group's last match finishes -- not all at once at the end
    # of the whole group stage. So each group's bonus is added at its own
    # resolving match, not lumped onto a single global "last group match".
    group_bonus_by_user_group, final_match_by_group = compute_group_position_bonus(
        results, prediction_by_user_match, participants
    )
    group_bonus_total_by_user: dict[int, int] = {p["userId"]: 0 for p in participants}
    group_bonus_at_match: dict[tuple[int, int], int] = {}
    for (uid, group_num), pts in group_bonus_by_user_group.items():
        group_bonus_total_by_user[uid] += pts
        match_id = final_match_by_group.get(group_num)
        if match_id is not None:
            group_bonus_at_match[(uid, match_id)] = group_bonus_at_match.get((uid, match_id), 0) + pts

    # 2. cumulative AND per-match (non-cumulative) series per participant, in
    # chronological order. matchPoints is what a single match actually
    # contributed (match points + any bonus resolved on it) -- useful for a
    # "here's what you earned in this game" view, separate from the running
    # total the line race needs. The line starts at 0 -- nothing is banked
    # as a starting balance; every point is attributed to the match (or
    # bonus event) that actually earned it.
    series = []
    for p in sorted(participants, key=lambda p: p["totalPoints"], reverse=True):
        uid = p["userId"]
        cumulative = []
        match_points = []
        result_types = []
        running = 0
        for m in matches:
            this_match = match_only_points[(uid, m["matchId"])] + topscorer_points[(uid, m["matchId"])]
            if m["matchId"] == (final_match["matchId"] if final_match else None):
                this_match += champion_bonus_by_user[uid]
            this_match += group_bonus_at_match.get((uid, m["matchId"]), 0)
            running += this_match
            match_points.append(this_match)
            cumulative.append(running)
            result_types.append(result_type.get((uid, m["matchId"])))
        series.append(
            {
                "userId": uid,
                "userName": p["userName"],
                "totalPoints": p["totalPoints"],
                "groupPositionBonus": group_bonus_total_by_user[uid],
                "computedTotal": running,
                "cumulative": cumulative,
                "matchPoints": match_points,
                "resultTypes": result_types,
            }
        )
    for i, s in enumerate(series, 1):
        s["rank"] = i

    # 3. QA check only -- surfaces anything still unexplained instead of
    # silently papering over it with a calibration fudge. Compares our
    # from-scratch total against both Scorito's overall leaderboard total
    # and its per-round scoreblock totals.
    for s in series:
        uid = s["userId"]
        gap = s["totalPoints"] - s["computedTotal"]
        # While a match is live, computed totals intentionally run ahead of
        # (or behind) Scorito's official numbers -- gaps are expected, not a
        # scoring-model bug, so don't drown the QA signal in them.
        if abs(gap) > 1 and not live_round_names:
            print(
                f"WARNING: {s['userName']} computed total {s['computedTotal']} "
                f"vs Scorito's official total {s['totalPoints']} (gap {gap:+d})"
            )
        for round_name, round_matches in _matches_by_round(matches).items():
            if round_name in live_round_names:
                continue
            official_total = official_round_points.get(uid, {}).get(round_name)
            if official_total is None:
                continue
            computed_round_total = sum(
                match_only_points[(uid, m["matchId"])] + topscorer_points[(uid, m["matchId"])]
                for m in round_matches
            )
            if round_name == "Ronde 3":
                computed_round_total += group_bonus_total_by_user[uid]
            gap = official_total - computed_round_total
            if abs(gap) > 1:
                print(
                    f"WARNING: {s['userName']} / {round_name}: computed "
                    f"{computed_round_total} vs official {official_total} (gap {gap:+d})"
                )

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
        "matches": match_index,
        "series": series,
        "notes": (
            "Cumulative points = match winner/exact-score points (computed "
            "directly from the pool's own scoring rules, scaled by round "
            "tier) + real topscorer-goal, champion-pick, and simulated "
            "group-position bonuses, each added at the match where it "
            "resolves. No calibration -- verified to reproduce Scorito's "
            "official per-round totals exactly."
        ),
    }


def main() -> None:
    with open(DATA_DIR / "shared.json", encoding="utf-8") as f:
        shared = json.load(f)
    with open(DATA_DIR / "predictions.json", encoding="utf-8") as f:
        preds = json.load(f)
    with open(ROOT / "pools.json", encoding="utf-8") as f:
        pools = json.load(f)

    for pool in pools:
        with open(DATA_DIR / "pools" / f"{pool['slug']}.json", encoding="utf-8") as f:
            pool_data = json.load(f)
        print(f"=== {pool['name']} ({pool['slug']}) ===")
        payload = build_payload(pool_data["participants"], shared, preds)
        out_path = DATA_DIR / "pools" / f"{pool['slug']}-computed.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(
            f"Wrote {out_path} ({len(payload['matches'])} matches, "
            f"{len(payload['series'])} participants)"
        )


if __name__ == "__main__":
    main()
