#!/usr/bin/env python3
# Manual local recording only; never part of CI or make check.
# Playwright is intentionally absent from the repository lock files. Install it
# outside the repository, for example:
#   python3 -m venv /tmp/s1-rec-venv
#   /tmp/s1-rec-venv/bin/pip install playwright
#   /tmp/s1-rec-venv/bin/python -m playwright install --with-deps chromium
# Run with an absolute path to the project's installed Python environment:
#   env -u OOS_LLM_API_KEY -u OPENAI_API_KEY OOS_LIVE_ENABLED=false \
#     /tmp/s1-rec-venv/bin/python scripts/record_offline_demo.py \
#       --app-python /absolute/path/to/project/.venv/bin/python
"""Record docs/OFFLINE_WALKTHROUGH.zh-CN.md using only synthetic UI input.

The app runs from a fresh private temporary directory under env -i, without
seeding. Only the browser viewport is recorded, never the desktop or terminal.
WebM, downloaded CSV, server logs and inspection frames stay in the temporary
directory; only the validated GIF is copied into docs/assets.
"""

import argparse
import csv
import io
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

try:
    from playwright.sync_api import expect, sync_playwright
except ModuleNotFoundError:
    raise SystemExit("Install Playwright in the external recording venv first") from None

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/offline-demo.gif"
WIDTH = 1000
HEIGHT = 800
SHIPPED_MARK = '"shipped_qty": 10'


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def wait_for_server(process, url):
    opener = build_opener(ProxyHandler({}))
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Isolated demo server stopped before startup")
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Isolated demo server did not become ready")


def select_option(panel, pattern):
    for select in panel.locator("select:visible").all():
        for option in select.locator("option").all():
            if re.search(pattern, option.inner_text()):
                select.select_option(value=option.get_attribute("value"))
                return
    raise RuntimeError("Required synthetic action option was not found")


def show_shipped_qty(locator):
    expect(locator).to_be_visible()
    found = locator.evaluate(
        """(el, mark) => {
          const text = el.value || "";
          const idx = text.indexOf(mark);
          if (idx < 0) return false;
          const lines = text.split("\\n");
          const line = text.slice(0, idx).split("\\n").length - 1;
          el.scrollTop = Math.max(0, (line - 2) * (el.scrollHeight / lines.length));
          return true;
        }""",
        SHIPPED_MARK,
    )
    if not found:
        raise RuntimeError("Synthetic snapshot did not include shipped_qty 10")
    locator.scroll_into_view_if_needed()


def record(work, base_url, chromium):
    blocked = []
    with sync_playwright() as playwright:
        launch = {
            "headless": True,
            # Container/sandbox hosts often reject a sandboxed Chromium.
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        if chromium:
            launch["executable_path"] = str(chromium)
        browser = playwright.chromium.launch(**launch)
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            record_video_dir=str(work / "video"),
            record_video_size={"width": WIDTH, "height": HEIGHT},
            locale="zh-CN",
            accept_downloads=True,
            service_workers="block",
        )

        def local_only(route):
            url = urlsplit(route.request.url)
            if (
                url.scheme != "http"
                or url.netloc != urlsplit(base_url).netloc
                or url.path.endswith("/extract")
            ):
                blocked.append(True)
                route.abort()
            else:
                route.continue_()

        context.route("**/*", local_only)
        page = context.new_page()
        page.set_default_timeout(8000)
        video = page.video
        try:
            page.goto(base_url + "/ui/reviews", wait_until="networkidle")
            expect(page.get_by_text("开始导入", exact=True)).to_be_visible()
            page.wait_for_timeout(1500)
            page.get_by_text("开始导入", exact=True).click()
            expect(page.locator("body")).to_contain_text("SYN-2401")
            help_box = page.locator("#offline-example-help")
            expect(help_box).to_contain_text("50 → 36")
            expect(help_box).to_contain_text("−14")
            help_box.scroll_into_view_if_needed()
            page.wait_for_timeout(2500)
            # The review UI stores shipped quantity in the synthetic snapshot
            # JSON, not as a standalone "已发 10" label. Scroll that field
            # into view so SYN-2401, ordered 50 and shipped 10 are visible.
            show_shipped_qty(page.locator("#snapshot"))
            page.wait_for_timeout(3000)
            page.get_by_text("保存资料，不执行订单", exact=True).click()
            page.get_by_text("人工输入/纠正", exact=True).click()
            save = page.get_by_role("button", name="保存全部人工动作并重新核对")
            expect(save).to_be_enabled()
            panel = save.locator("xpath=ancestor::form[1]")
            select_option(panel, r"00123\s*[·・]\s*藏蓝\s*[·・]\s*M")
            select_option(panel, r"总量改成")
            quantity = panel.locator('input[type="number"]:visible')
            expect(quantity).to_have_count(1)
            quantity.fill("36")
            reason = panel.get_by_label(re.compile("理由|原因"))
            if reason.count() == 0:
                reason = panel.locator("#reason")
            expect(reason).to_have_count(1)
            reason.fill("核对合成示例")
            page.wait_for_timeout(2000)
            save.click()

            row = (
                page.get_by_role("table")
                .filter(has_text="规则计算的逐行差异")
                .get_by_role("row")
                .filter(has_text="00123")
                .filter(has_text="藏蓝")
            )
            expect(row).to_have_count(1)
            expect(row.get_by_role("cell", name="50", exact=True)).to_have_count(1)
            expect(row.get_by_role("cell", name="36", exact=True)).to_have_count(1)
            expect(row.get_by_role("cell", name=re.compile(r"^[−\-]14$"))).to_have_count(1)
            expect(page.locator("body")).to_contain_text("人工")
            row.scroll_into_view_if_needed()
            page.wait_for_timeout(3500)
            details = page.get_by_text("查看或更新业务快照 JSON", exact=True)
            if details.count() == 0:
                raise RuntimeError("Snapshot details control was not found")
            details.first.click()
            show_shipped_qty(page.locator('textarea[name="snapshot"]'))
            page.wait_for_timeout(3000)
            row.scroll_into_view_if_needed()
            page.wait_for_timeout(2500)

            page.get_by_text("确认这份处理清单（不提交订单）", exact=True).click()
            heading = page.get_by_role("heading", level=1)
            expect(heading).to_contain_text("SYN-2401")
            expect(heading).to_contain_text("未执行订单")
            heading.scroll_into_view_if_needed()
            page.wait_for_timeout(2500)
            download_link = page.get_by_role("link", name=re.compile("CSV"))
            if download_link.count() == 0:
                download_link = page.get_by_role("button", name=re.compile("CSV"))
            expect(download_link).to_have_count(1)
            download_link.scroll_into_view_if_needed()
            expect(page.get_by_role("heading", name="清单已确认，订单未执行")).to_be_visible()
            page.wait_for_timeout(2000)
            with page.expect_download() as downloaded:
                download_link.click()
            download = downloaded.value
            if download.failure():
                raise RuntimeError("Synthetic CSV download failed")
            csv_path = work / "checklist.csv"
            download.save_as(csv_path)
            rows = list(csv.reader(io.StringIO(csv_path.read_text("utf-8-sig"))))
            matching = [r for r in rows if any("00123" in cell for cell in r)]
            if len(matching) != 1 or not {"50", "36", "-14"}.issubset(
                {cell.strip() for cell in matching[0]}
            ):
                raise RuntimeError("Downloaded CSV quantities did not match")
            if "未执行订单" not in csv_path.read_text("utf-8-sig"):
                raise RuntimeError("Downloaded CSV did not state its execution boundary")
            page.wait_for_timeout(2500)
            if blocked:
                raise RuntimeError("Recording attempted a forbidden request")
        finally:
            context.close()
            browser.close()
        return Path(video.path())


def convert(video, work):
    gif = work / "offline-demo.gif"
    run(
        [
            "/usr/bin/ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-filter_complex",
            "fps=8,scale=1000:-1:flags=lanczos,split[a][b];"
            "[a]palettegen=stats_mode=diff:max_colors=128[p];"
            "[b][p]paletteuse=dither=bayer",
            "-loop",
            "0",
            str(gif),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(gif),
        ],
        capture_output=True,
        text=True,
    )
    probe = json.loads(result.stdout)
    duration = float(probe["format"]["duration"])
    width = probe["streams"][0]["width"]
    size = gif.stat().st_size
    if not (0 < duration <= 120 and width <= 1000 and size <= 8_000_000):
        raise RuntimeError("Recording exceeded the GIF duration, width or size limit")
    # Sample the final GIF, not only the source WebM, for manual visual review.
    inspect = Path("/tmp/s1-inspect")
    if inspect.exists():
        shutil.rmtree(inspect)
    inspect.mkdir(parents=True)
    for index, fraction in enumerate((0.08, 0.22, 0.38, 0.55, 0.72, 0.90)):
        frame = inspect / f"frame-{index}.png"
        run(
            [
                "/usr/bin/ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(duration * fraction),
                "-i",
                str(gif),
                "-frames:v",
                "1",
                str(frame),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.copyfile(frame, work / f"frame-{index}.png")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(gif, OUTPUT)
    print(f"GIF: {size} bytes; {duration:.2f} seconds; {width}px wide")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-python", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, help="Optional installed Chromium")
    args = parser.parse_args()
    # Keep the venv symlink: resolving it would lose its installed packages.
    app_python = args.app_python.absolute()
    if not app_python.is_file():
        parser.error("The installed application Python is unavailable")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    work = Path(tempfile.mkdtemp(prefix="order-ops-preview."))
    isolated = [
        "env",
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "LANG=C.UTF-8",
        f"OOS_DATABASE_URL=sqlite:///{work / 'review.db'}",
        "OOS_LIVE_ENABLED=false",
        str(app_python),
        "-I",
        "-m",
    ]
    with (work / "server.log").open("wb") as log:
        run(isolated + ["app.cli", "init-db"], cwd=work, stdout=log, stderr=log)
        process = subprocess.Popen(
            isolated + ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=work,
            stdout=log,
            stderr=log,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            wait_for_server(process, base_url + "/ui/reviews")
            video = record(work, base_url, args.chromium)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    convert(video, work)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Subprocess exceptions can include the complete env -i command. Keep
        # all startup details out of terminal output as well as the recording.
        print(f"Offline recording failed ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
