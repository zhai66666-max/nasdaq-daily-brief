"""纳斯达克100 每日简报 —— 统一入口。

把原来三个独立项目（三个仓库、三封邮件）合并成一次运行、一封邮件：

    nasdaq-etf-monitor      → 纳指历史回撤 × 国内纳指ETF溢价排名 × 加仓信号
    global-drawdown-radar   → 全球 11 类资产回撤雷达 × 新信号
    nasdaq100-daily-report  → 纳指行情 / 宏观仪表盘 / 技术指标 / 成分股 / AI 深度分析

用法：
    python -m src.main --preview            # 抓真实数据 → 渲染 preview.html，不发信
    python -m src.main --dry-run            # 同上，且打开浏览器预览
    python -m src.main                      # 抓数据 → 渲染 → 发邮件 → 归档
    python -m src.main --no-ai              # 跳过 AI 调用（省 token，调试用）
    python -m src.main --check-smtp         # 只验证 SMTP 账号能不能登录
    python -m src.main --replay <html>      # 用已有 HTML 补发一次（不重新抓数据）

环境变量见 .env.example；两套旧命名（EMAIL_* / SENDER_*）都兼容。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime

# 允许 `python src/main.py` 和 `python -m src.main` 两种跑法
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:                                  # 没装 dotenv 也能跑
    pass

from src import ai as ai_mod
from src import email_sender, pipeline, render
from src.paths import ARCHIVE_DIR, REPO_ROOT, TEMPLATES_DIR
from src.settings import deepseek_key, github_actions, load_smtp

logger = logging.getLogger("brief")

BRIEF_NAME = "纳斯达克100 每日简报"


# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # 第三方库太吵
    for noisy in ("urllib3", "yfinance", "peewee", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def subject_for(run_date: str, ctx: dict) -> str:
    """标题带上当天最关键的结论，方便在收件箱列表里直接看出要不要点开。"""
    s = ctx.get("summary", {})
    parts = [f"{BRIEF_NAME} · {run_date}"]

    tag = []
    if s.get("ndx_change_pct") and s["ndx_change_pct"] != "—":
        tag.append(f"纳指 {s['ndx_price']} {s['ndx_change_pct']}")
    if s.get("hist_dd_display") and s["hist_dd_display"] != "—":
        tag.append(f"回撤 {s['hist_dd_display']}")
    alert = s.get("qqq_alert") or {}
    if alert.get("badge"):
        tag.append(f"⚠ {alert['badge']}")
    if s.get("global_historic"):
        tag.append(f"历史级回撤 {s['global_historic']}")
    elif s.get("global_alerts"):
        tag.append(f"新信号 {s['global_alerts']}")

    if tag:
        parts.append(" | ".join(tag))
    return "  ｜  ".join(parts)


def archive(html: str, run_date: str, subject: str) -> str:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"brief_{run_date}.html"
    path.write_text(html, encoding="utf-8")
    logger.info("已归档：%s（%.0f KB）", path, len(html.encode("utf-8")) / 1024)
    return str(path)


# ─────────────────────────────────────────────────────────────────────────────

def build(args) -> tuple[str, dict, str]:
    """抓数据 → AI → 渲染。返回 (html, context, subject)。失败不抛，尽量降级。"""
    run_date = pipeline.beijing_date_str()
    t0 = time.time()

    # 1) 四个来源并发抓取（单源失败被隔离）
    logger.info("─" * 62)
    logger.info("① 采集数据（4 个来源并发）")
    results = pipeline.collect_all(run_date)
    ok_n = sum(1 for r in results.values() if r.ok)
    logger.info("   完成：%d/%d 个来源成功，耗时 %.1fs", ok_n, len(results), time.time() - t0)

    # 2) AI 解读（缺 key 或缺数据自动跳过）
    ai_out = {"analysis_sections": [], "commentary": None}
    key = deepseek_key()
    if args.no_ai:
        logger.info("② AI 解读：已按 --no-ai 跳过")
    elif not key:
        logger.info("② AI 解读：未配置 DEEPSEEK_API_KEY，跳过（其余内容不受影响）")
    elif ok_n == 0:
        logger.info("② AI 解读：全部数据源失败，跳过")
    else:
        logger.info("② AI 解读（DeepSeek %s）", key[:6] + "…")
        t1 = time.time()
        ai_out = ai_mod.run_all(
            results["nasdaq100"].data if results["nasdaq100"].ok else None,
            results["drawdown_radar"].data if results["drawdown_radar"].ok else None,
            key,
            render.load_display().get("thresholds"),
        )
        logger.info("   AI 完成，耗时 %.1fs", time.time() - t1)

    # 3) 渲染
    logger.info("③ 渲染邮件")
    ctx = render.build_context(results, ai_out)
    html = render.render(ctx)
    subject = subject_for(run_date, ctx)
    logger.info("   标题：%s", subject)
    logger.info("   正文 %.0f KB", len(html.encode("utf-8")) / 1024)

    return html, ctx, subject


def cmd_check_smtp() -> int:
    cfg = load_smtp()
    if not cfg.complete:
        logger.error("SMTP 配置不完整，缺失：%s", "、".join(cfg.missing))
        logger.error("请参考 .env.example 补齐（两套命名任选一套）")
        return 2
    logger.info("配置：%s", cfg.mask())
    ok = email_sender.dry_run_check(cfg)
    logger.info("SMTP 登录%s", "成功 ✓" if ok else "失败 ✗")
    return 0 if ok else 1


def cmd_replay(path: str, do_send: bool) -> int:
    cfg = load_smtp()
    if not cfg.complete:
        logger.error("SMTP 配置不完整，缺失：%s", "、".join(cfg.missing))
        return 2
    with open(path, encoding="utf-8") as f:
        html = f.read()
    run_date = datetime.now().strftime("%Y-%m-%d")
    subject = f"{BRIEF_NAME} · {run_date}（补发）"
    if not do_send:
        logger.info("--no-email：仅校验，不发送")
        return 0
    logger.info("补发：%s", cfg.mask())
    return 0 if email_sender.send(cfg, subject, html) else 1


# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=f"{BRIEF_NAME} —— 统一入口")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--preview", action="store_true",
                   help="渲染 preview.html，不发信")
    g.add_argument("--dry-run", action="store_true",
                   help="渲染并自动打开浏览器预览，不发信")
    g.add_argument("--check-smtp", action="store_true",
                   help="只验证 SMTP 登录，不抓数据")
    g.add_argument("--replay", metavar="HTML",
                   help="用已有 HTML 补发一封，不重新抓数据")
    p.add_argument("--no-email", action="store_true", help="跑完整流程但不发信")
    p.add_argument("--no-ai", action="store_true", help="跳过 AI 调用")
    p.add_argument("--no-archive", action="store_true", help="不写归档文件")
    p.add_argument("--open", action="store_true", help="渲染后打开浏览器")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    args = p.parse_args(argv)

    setup_logging(args.verbose)
    logger.info("%s 启动（%s）", BRIEF_NAME, "GitHub Actions" if github_actions() else "本地")

    if args.check_smtp:
        return cmd_check_smtp()
    if args.replay:
        return cmd_replay(args.replay, do_send=not args.no_email)

    t_all = time.time()
    try:
        html, ctx, subject = build(args)
    except Exception as exc:                    # noqa: BLE001
        logger.exception("构建失败：%s", exc)
        return 1

    # 未成功抓取到任何来源时，宁可报错也不发一封空邮件（保留旧行为）
    if not any(r.get("ok") for r in ctx["sources"]):
        logger.error("全部数据源失败，放弃发送以避免推送空简报")
        if args.preview or args.dry_run:
            out = REPO_ROOT / "preview.html"
            out.write_text(html, encoding="utf-8")
            logger.info("仍写出调试用预览：%s", out)
        return 1

    # 失败来源逐个提示
    failed = [r for r in ctx["sources"] if not r["ok"]]
    if failed:
        for r in failed:
            logger.warning("来源失败：%s — %s", r["label"], r["error"][:200])

    # ── 预览模式
    if args.preview or args.dry_run:
        out = REPO_ROOT / "preview.html"
        out.write_text(html, encoding="utf-8")
        logger.info("④ 预览已写出：%s", out)
        if args.dry_run or args.open:
            _open_browser(out)
        logger.info("用时 %.1fs —— 未发送邮件（%s）",
                    time.time() - t_all, "--preview/--dry-run")
        return 0

    # ── 归档
    if not args.no_archive:
        archive(html, ctx["meta"]["date"], subject)

    # ── 发信
    if args.no_email:
        logger.info("④ --no-email：跳过发送")
        return 0

    cfg = load_smtp()
    if not cfg.complete:
        logger.error("SMTP 配置不完整，缺失：%s", "、".join(cfg.missing))
        logger.error("参考 .env.example；或用 --preview 先看效果")
        return 2

    logger.info("④ 发送邮件：%s", cfg.mask())
    ok = email_sender.send(cfg, subject, html,
                           attachments={f"brief_{ctx['meta']['date']}.html":
                                        html.encode("utf-8")})
    logger.info("总用时 %.1fs —— %s", time.time() - t_all, "成功 ✓" if ok else "失败 ✗")
    return 0 if ok else 1


def _open_browser(path) -> None:
    import subprocess
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(str(path))                 # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


if __name__ == "__main__":
    raise SystemExit(main())
