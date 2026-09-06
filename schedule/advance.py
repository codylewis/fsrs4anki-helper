import time
from anki.decks import DeckManager
from anki.utils import ids2str
from aqt.utils import tooltip, showWarning, getText
from anki.stats import QUEUE_TYPE_REV
from ..configuration import Config
from ..i18n import t
from ..utils import *


def get_due_per_day_breakdown(did, num_days=7):
    DM = DeckManager(mw.col)
    if did is not None:
        did_list = ids2str(DM.deck_and_child_ids(did))

    today = mw.col.sched.today
    rows = mw.col.db.all(f"""
        SELECT
            due - {today},
            json_extract(data, '$.s'),
            CASE WHEN odid==0
            THEN {today} - (due - ivl)
            ELSE {today} - (odue - ivl)
            END,
            json_extract(data, '$.dr'),
            COALESCE(json_extract(data, '$.decay'), 0.5)
        FROM cards
        WHERE data != ''
        AND json_extract(data, '$.s') IS NOT NULL
        AND json_extract(data, '$.dr') IS NOT NULL
        AND due > {today}
        AND due <= {today + num_days}
        AND queue = {QUEUE_TYPE_REV}
        {"AND did IN %s" % did_list if did is not None else ""}
    """)
    safe_counts = {}
    for day, stability, elapsed, desired_r, decay in rows:
        current_r = power_forgetting_curve(max(elapsed, 0), stability, -decay)
        deficit = 1 - (current_r ** (-1 / decay) - 1) / (desired_r ** (-1 / decay) - 1)
        if deficit < 0.13:
            safe_counts[day] = safe_counts.get(day, 0) + 1

    lines = []
    running_total = 0
    for day in range(1, num_days + 1):
        running_total += safe_counts.get(day, 0)
        lines.append(f"{day} ({running_total})")
    return "\n".join(lines)


def get_desired_days_limit_with_response(did):
    label = (
        t("advance-days-label")
        + "\n"
        + t("advance-days-breakdown-label")
        + "\n"
        + get_due_per_day_breakdown(did)
        + "\n\n"
        + t("advance-days-next-step-text")
    )
    s, r = getText(label, default="0")
    if r:
        return (RepresentsInt(s), r)
    return (None, r)


def get_desired_advance_cnt_with_response(safe_cnt, did, days_limit=0):
    inquire_text = t("advance-inquire-text") + "\n"
    if days_limit > 0:
        notification_key = (
            "advance-notification-text-deck-days-limited"
            if did
            else "advance-notification-text-collection-days-limited"
        )
    else:
        notification_key = (
            "advance-notification-text-deck"
            if did
            else "advance-notification-text-collection"
        )
    notification_text = (
        t(notification_key, count=safe_cnt, days=days_limit) + "\n"
    )
    warning_text = t("advance-warning-text")
    info_text = t("advance-info-text")
    default_cnt = safe_cnt if days_limit > 0 else min(safe_cnt, 10)
    s, r = getText(
        inquire_text + notification_text + warning_text + info_text,
        default=f"{default_cnt}",
    )
    if r:
        return (RepresentsInt(s), r)
    return (None, r)


def advance(did):
    if not mw.col.get_config("fsrs"):
        tooltip(t("enable-fsrs-warning"))
        return

    config = Config()
    config.load()
    days_limit = 0
    if config.advance_days_filter_enabled:
        days_limit, resp = get_desired_days_limit_with_response(did)
        if days_limit is None or days_limit < 0:
            if resp:
                showWarning(t("advance-days-enter-number"))
            return

    DM = DeckManager(mw.col)
    if did is not None:
        did_list = ids2str(DM.deck_and_child_ids(did))

    date_filter = (
        f"AND due > {mw.col.sched.today} AND due <= {mw.col.sched.today + days_limit}"
        if days_limit > 0
        else f"AND due > {mw.col.sched.today}"
    )

    cards = mw.col.db.all(f"""
        SELECT 
            id, 
            CASE WHEN odid==0
            THEN did
            ELSE odid
            END,
            ivl,
            json_extract(data, '$.s'),
            CASE WHEN odid==0
            THEN {mw.col.sched.today} - (due - ivl)
            ELSE {mw.col.sched.today} - (odue - ivl)
            END,
            json_extract(data, '$.dr'),
            COALESCE(json_extract(data, '$.decay'), 0.5)
        FROM cards
        WHERE data != ''
        AND json_extract(data, '$.s') IS NOT NULL
        AND json_extract(data, '$.dr') IS NOT NULL
        {date_filter}
        AND queue = {QUEUE_TYPE_REV}
        {"AND did IN %s" % did_list if did is not None else ""}
    """)
    # x[0]: cid
    # x[1]: did
    # x[2]: interval
    # x[3]: stability
    # x[4]: elapsed days
    # x[5]: desired retention
    # x[6]: decay
    # x[7]: current retention
    cards = map(
        lambda x: (
            x
            + [
                power_forgetting_curve(max(x[4], 0), x[3], -x[6]),
            ]
        ),
        cards,
    )

    # sort by (1 - elapsed_day / scheduled_day)
    # = 1-ln(current retention)/ln(requested retention), -stability (ascending)
    cards = sorted(
        cards,
        key=lambda x: (
            1 - (x[7] ** (-1 / x[6]) - 1) / (x[5] ** (-1 / x[6]) - 1),
            -x[3],
        ),
    )
    safe_cnt = len(
        list(
            filter(
                lambda x: (
                    1 - (x[7] ** (-1 / x[6]) - 1) / (x[5] ** (-1 / x[6]) - 1) < 0.13
                ),
                cards,
            )
        )
    )

    desired_advance_cnt, resp = get_desired_advance_cnt_with_response(
        safe_cnt, did, days_limit
    )
    if desired_advance_cnt is None:
        if resp:
            showWarning(t("advance-enter-number"))
        return
    else:
        if desired_advance_cnt <= 0:
            showWarning(t("advance-positive-integer"))
            return

    cnt = 0
    new_target_rs = []
    prev_target_rs = []
    advanced_cards = []
    start_time = time.time()
    undo_entry = mw.col.add_custom_undo_entry(t("advance"))
    for cid, did, ivl, stability, _, _, decay, _ in cards:
        if cnt >= desired_advance_cnt:
            break

        card = mw.col.get_card(cid)
        last_review, _ = get_last_review_date_and_interval(card)
        new_ivl = mw.col.sched.today - last_review
        card = update_card_due_ivl(card, new_ivl)
        write_custom_data(card, "v", "advance")
        advanced_cards.append(card)
        prev_target_rs.append(power_forgetting_curve(ivl, stability, -decay))
        new_target_rs.append(power_forgetting_curve(new_ivl, stability, -decay))
        cnt += 1

    mw.col.update_cards(advanced_cards)
    mw.col.merge_undo_entries(undo_entry)
    result_text = t("advance-result-text", count=cnt)
    if len(new_target_rs) > 0 and len(prev_target_rs) > 0:
        result_text += t(
            "advance-retention-change",
            prev_retention=f"{sum(prev_target_rs) / len(prev_target_rs):.2f}",
            new_retention=f"{sum(new_target_rs) / len(new_target_rs):.2f}",
        )

    tooltip(result_text)
    mw.reset()
