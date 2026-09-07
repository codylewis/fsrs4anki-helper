import time
from anki.decks import DeckManager
from anki.utils import ids2str
from aqt.utils import tooltip, showWarning, getText
from anki.stats import QUEUE_TYPE_REV
from ..configuration import Config
from ..i18n import t
from ..utils import *


SAFE_RETENTION_DEFICIT = 0.13


def get_current_retention(elapsed, stability, decay):
    return power_forgetting_curve(max(elapsed, 0), stability, -decay)


def get_retention_deficit(current_r, desired_r, decay):
    return 1 - (current_r ** (-1 / decay) - 1) / (desired_r ** (-1 / decay) - 1)


def fetch_cards_with_deficit(did, max_due_day=None):
    """One shared query: cards due after today (optionally capped at
    today + max_due_day), each annotated with its day offset and
    retention deficit."""
    DM = DeckManager(mw.col)
    if did is not None:
        did_list = ids2str(DM.deck_and_child_ids(did))

    today = mw.col.sched.today
    upper_bound = f"AND due <= {today + max_due_day}" if max_due_day else ""

    rows = mw.col.db.all(f"""
        SELECT
            id,
            due - {today},
            CASE WHEN odid==0 THEN did ELSE odid END,
            ivl,
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
        {upper_bound}
        AND queue = {QUEUE_TYPE_REV}
        {"AND did IN %s" % did_list if did is not None else ""}
    """)

    cards = []
    for cid, day_offset, card_did, ivl, stability, elapsed, desired_r, decay in rows:
        current_r = get_current_retention(elapsed, stability, decay)
        deficit = get_retention_deficit(current_r, desired_r, decay)
        cards.append((cid, day_offset, card_did, ivl, stability, decay, deficit))
    return cards


def get_due_per_day_breakdown(did, num_days=7):
    cards = fetch_cards_with_deficit(did, max_due_day=num_days)
    safe_counts = {}
    for _, day_offset, _, _, _, _, deficit in cards:
        if deficit < SAFE_RETENTION_DEFICIT:
            safe_counts[day_offset] = safe_counts.get(day_offset, 0) + 1

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

    cards = fetch_cards_with_deficit(did, max_due_day=days_limit or None)
    # x[0]: cid
    # x[1]: day offset
    # x[2]: did
    # x[3]: interval
    # x[4]: stability
    # x[5]: decay
    # x[6]: retention deficit

    # sort by (1 - elapsed_day / scheduled_day)
    # = 1-ln(current retention)/ln(requested retention), -stability (ascending)
    cards = sorted(cards, key=lambda x: (x[6], -x[4]))
    safe_cnt = len([c for c in cards if c[6] < SAFE_RETENTION_DEFICIT])

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
    for cid, _, card_did, ivl, stability, decay, _ in cards:
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
