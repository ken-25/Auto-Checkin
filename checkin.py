"""会員サイトのチェックインボタンを1日1回押す自動化スクリプト。

GitHub Actions から実行される想定。必要な環境変数:
  USER_NAME / USER_PASSWORD / TARGET_URL  … Secrets から渡す
任意の環境変数（サイトの作りに合わせて上書きしたい場合のみ。Variables で設定）:
  LOGIN_URL, USER_SELECTOR, PASS_SELECTOR, SUBMIT_SELECTOR,
  CHECKIN_SELECTOR, CHECKIN_TEXTS, ALREADY_TEXTS, SUCCESS_TEXTS, MAINTENANCE_TEXTS, DEBUG_DUMP

終了コード: 0 = 押した／既に押し済み／サイトがメンテナンス中（警告のみ）、 1 = 失敗（要調査）
"""

import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------- 設定読み込み

TARGET_URL = os.environ.get("TARGET_URL", "").strip() or "https://sp.aimyong.net/visit/"
LOGIN_URL = os.environ.get("LOGIN_URL", "").strip()
USER_NAME = os.environ.get("USER_NAME", "")
USER_PASSWORD = os.environ.get("USER_PASSWORD", "")

USER_SELECTOR = os.environ.get("USER_SELECTOR", "").strip()
PASS_SELECTOR = os.environ.get("PASS_SELECTOR", "").strip()
SUBMIT_SELECTOR = os.environ.get("SUBMIT_SELECTOR", "").strip()
CHECKIN_SELECTOR = os.environ.get("CHECKIN_SELECTOR", "").strip()

DEBUG_DUMP = os.environ.get("DEBUG_DUMP", "").lower() in ("1", "true", "yes")

ATTEMPTS = 3


def _list_from_env(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [s.strip() for s in raw.split(",") if s.strip()]


# ボタンらしきものを探すときの手がかり（部分一致・NFKC正規化・小文字化して比較）
CHECKIN_TEXTS = _list_from_env(
    "CHECKIN_TEXTS",
    [
        "チェックイン", "ﾁｪｯｸｲﾝ", "checkin", "check in", "check-in",
        "訪問する", "来訪", "訪問", "スタンプ", "押す", "出席", "ログインボーナス",
    ],
)

# 「訪問履歴」のようなナビゲーションリンクを誤クリックしないための除外語
EXCLUDE_TEXTS = _list_from_env(
    "EXCLUDE_TEXTS",
    ["履歴", "一覧", "ランキング", "ヘルプ", "help", "使い方", "とは", "について", "設定", "ログアウト"],
)

# クリック前に見つかったら「今日はもう押してある」と判断する語
ALREADY_TEXTS = _list_from_env(
    "ALREADY_TEXTS",
    [
        "チェックイン済", "訪問済", "本日は済", "本日分", "済みです", "すでに", "既に",
        "また明日", "明日また", "明日また来て", "already",
    ],
)

# クリック後に見つかったら成功と判断する語
SUCCESS_TEXTS = _list_from_env(
    "SUCCESS_TEXTS",
    ALREADY_TEXTS + ["ありがとう", "完了", "しました", "ポイント", "獲得", "thank"],
)


# ボタンが見つからないときにこれがあれば「サイトがメンテナンス中」と判断する語
MAINTENANCE_TEXTS = _list_from_env(
    "MAINTENANCE_TEXTS",
    [
        "メンテナンス中", "システムメンテナンス", "メンテナンスを実施", "メンテナンスのため",
        "サービスを一時停止", "一時的にご利用いただけません", "ただいまご利用いただけません",
        "maintenance",
    ],
)

# この HTTP ステータスが返ってきたらメンテナンス／一時停止とみなす
MAINTENANCE_STATUSES = (502, 503, 504)


class MaintenanceError(Exception):
    """サイトがメンテナンス中などで、今回はチェックインできない状態。リトライしても無駄なので即終了する。"""


# ------------------------------------------------------------------ ユーティリティ

def log(msg):
    print(f"[{datetime.now(JST):%Y-%m-%d %H:%M:%S} JST] {msg}", flush=True)


def scrub(text):
    """ログに出す前に認証情報を伏せる（Actions 側のマスクに加えた二重の保険）。"""
    if not text:
        return text
    for secret in (USER_PASSWORD, USER_NAME):
        if secret and len(secret) >= 3:
            text = text.replace(secret, "***")
    return text


def norm(text):
    """全角/半角・大文字小文字・空白の揺れを吸収して比較しやすくする。"""
    if not text:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).lower()


def contains_any(haystack, needles):
    h = norm(haystack)
    return next((n for n in needles if norm(n) and norm(n) in h), None)


def page_text(page):
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:
        return ""


def check_response(response, url):
    """goto の結果がメンテナンスを示すステータスなら MaintenanceError を投げる。"""
    if response is not None and response.status in MAINTENANCE_STATUSES:
        raise MaintenanceError(f"{url} が HTTP {response.status} を返しました")


def goto(page, url):
    check_response(page.goto(url, wait_until="domcontentloaded"), url)
    settle(page)


def is_login_page(page):
    try:
        return page.locator('input[type="password"]').count() > 0
    except Exception:
        return False


# ------------------------------------------------------------------ ログイン処理

def do_login(page):
    """ID / パスワードのみの単純なログインフォームを埋めて送信する。"""
    log("ログイン画面と判断。フォームを送信します。")

    pass_loc = page.locator(PASS_SELECTOR) if PASS_SELECTOR else page.locator('input[type="password"]')
    pass_loc = pass_loc.first
    pass_loc.wait_for(state="visible", timeout=15000)

    if USER_SELECTOR:
        user_loc = page.locator(USER_SELECTOR).first
    else:
        # パスワード欄と同じ form の中にある最初のテキスト系入力欄をIDとみなす
        form = page.locator('form:has(input[type="password"])').first
        scope = form if form.count() > 0 else page
        user_loc = scope.locator(
            'input[type="text"], input[type="email"], input[type="tel"], input[type="number"], input:not([type])'
        ).first

    user_loc.wait_for(state="visible", timeout=15000)
    user_loc.fill(USER_NAME)
    pass_loc.fill(USER_PASSWORD)

    if SUBMIT_SELECTOR:
        submit = page.locator(SUBMIT_SELECTOR).first
    else:
        form = page.locator('form:has(input[type="password"])').first
        scope = form if form.count() > 0 else page
        submit = scope.locator(
            'input[type="submit"], button[type="submit"], button:not([type]), input[type="image"]'
        ).first

    try:
        if submit.count() > 0:
            submit.click(timeout=10000)
        else:
            pass_loc.press("Enter")
    except PlaywrightTimeoutError:
        pass_loc.press("Enter")

    settle(page)

    if is_login_page(page):
        raise RuntimeError(
            "ログインに失敗しました（送信後もログイン画面のままです）。"
            "USER_NAME / USER_PASSWORD、または USER_SELECTOR / PASS_SELECTOR / SUBMIT_SELECTOR を確認してください。"
        )
    log(f"ログイン成功。現在のURL: {page.url}")


def settle(page):
    """遷移とAjaxが落ち着くのを待つ。"""
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1500)


# ------------------------------------------------------- チェックインボタンの探索

CLICKABLE = 'button, a, input[type="submit"], input[type="button"], input[type="image"], [role="button"], [onclick]'


def label_of(handle):
    """要素の「見た目のラベル」をかき集める。"""
    parts = []
    for getter in (
        lambda: handle.inner_text(),
        lambda: handle.get_attribute("value"),
        lambda: handle.get_attribute("alt"),
        lambda: handle.get_attribute("aria-label"),
        lambda: handle.get_attribute("title"),
    ):
        try:
            v = getter()
        except Exception:
            v = None
        if v:
            parts.append(v.strip())
    return " ".join(parts)[:120]


def find_checkin_button(page):
    """チェックインボタンらしき要素を返す。見つからなければ (None, ラベル一覧)。"""
    if CHECKIN_SELECTOR:
        loc = page.locator(CHECKIN_SELECTOR).first
        if loc.count() > 0 and loc.is_visible():
            return loc, []
        return None, []

    labels = []
    elements = page.locator(CLICKABLE)
    for i in range(min(elements.count(), 200)):
        el = elements.nth(i)
        try:
            if not el.is_visible():
                continue
        except Exception:
            continue
        label = label_of(el)
        if label:
            labels.append(label)
        if contains_any(label, EXCLUDE_TEXTS):
            continue
        if contains_any(label, CHECKIN_TEXTS):
            return el, labels
    return None, labels


def report_unknown(page, labels):
    """ボタンも「済み」表示も見つからなかったときの手がかりを出す（個人情報は極力伏せる）。"""
    log("!! チェックインボタンも『済み』表示も見つかりませんでした。")
    log(f"   URL  : {page.url}")
    try:
        log(f"   Title: {page.title()}")
    except Exception:
        pass
    if labels:
        log("   ページ上のクリック可能な要素のラベル一覧:")
        for label in labels[:40]:
            log(f"     - {scrub(label)}")
    if DEBUG_DUMP:
        log("   --- ページ本文（DEBUG_DUMP 有効時のみ） ---")
        log(scrub(page_text(page))[:3000])
    else:
        log("   本文を見たい場合は workflow_dispatch の debug を on にして再実行してください"
            "（公開ログに個人情報が出る可能性がある点に注意）。")


# ------------------------------------------------------------------- メイン処理

def run_once(playwright):
    browser = playwright.chromium.launch(args=["--no-sandbox"])
    context = browser.new_context(**playwright.devices["iPhone 13"], locale="ja-JP",
                                  timezone_id="Asia/Tokyo")
    context.set_default_timeout(30000)
    page = context.new_page()
    try:
        start_url = LOGIN_URL or TARGET_URL
        log(f"アクセス: {start_url}")
        goto(page, start_url)

        if is_login_page(page):
            do_login(page)

        if norm(page.url) != norm(TARGET_URL):
            log(f"チェックインページへ移動: {TARGET_URL}")
            goto(page, TARGET_URL)

        if is_login_page(page):
            # ログイン直後にまた飛ばされた場合（セッションが確立していない）
            do_login(page)
            goto(page, TARGET_URL)

        button, labels = find_checkin_button(page)

        if button is None:
            text = page_text(page)
            hit = contains_any(text, ALREADY_TEXTS)
            if hit:
                log(f"本日は既にチェックイン済みでした（判定語: {hit}）。何もせず終了します。")
                return True
            hit = contains_any(text, MAINTENANCE_TEXTS)
            if hit:
                raise MaintenanceError(f"ページに『{hit}』の表示があります（URL: {page.url}）")
            report_unknown(page, labels)
            return False

        log(f"チェックインボタンを発見: 「{scrub(label_of(button))}」 → クリックします")
        button.click()
        settle(page)

        text_after = page_text(page)
        hit = contains_any(text_after, SUCCESS_TEXTS)
        still_there, _ = find_checkin_button(page)

        if hit:
            log(f"チェックイン成功（判定語: {hit}）。")
            return True
        if still_there is None:
            log("チェックイン成功（ボタンが消えたことを確認）。")
            return True

        log("!! クリックはしましたが、成功したか確認できませんでした。")
        if DEBUG_DUMP:
            log(scrub(text_after)[:3000])
        return False
    finally:
        context.close()
        browser.close()


def main():
    missing = [k for k in ("USER_NAME", "USER_PASSWORD") if not os.environ.get(k)]
    if missing:
        log(f"必須の Secrets が未設定です: {', '.join(missing)}")
        return 1

    log(f"対象URL: {TARGET_URL}")
    with sync_playwright() as playwright:
        for attempt in range(1, ATTEMPTS + 1):
            log(f"--- 試行 {attempt}/{ATTEMPTS} ---")
            try:
                if run_once(playwright):
                    return 0
            except MaintenanceError as exc:
                # メンテナンスはこちらの不具合ではないので失敗扱いにしない（次回の定時実行に任せる）
                log(f"サイトがメンテナンス中のため、今回はチェックインをスキップします: {scrub(str(exc))}")
                print("::warning title=サイトメンテナンス中::"
                      "チェックインできませんでした。次回の定時実行で再試行されます。"
                      "必要ならメンテナンス明けに手動実行してください。", flush=True)
                return 0
            except Exception as exc:  # noqa: BLE001 - 原因を出して次の試行へ
                log(f"エラー: {scrub(f'{type(exc).__name__}: {exc}')}")
            if attempt < ATTEMPTS:
                wait = 10 * attempt
                log(f"{wait} 秒待って再試行します。")
                time.sleep(wait)
    log("すべての試行が失敗しました。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
