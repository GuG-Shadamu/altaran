"""Sample data for previewing the dashboard without any Gmail access."""
from datetime import datetime, timedelta, timezone

from . import db
from .config import Account


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(cfg):
    conn = db.connect(cfg.db_path)
    if conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]:
        raise SystemExit("这个数据库里已经有真实邮件，demo 不会覆盖它。换一个 db_path 再试。")
    if not cfg.accounts:
        cfg.accounts = [Account("me@gmail.com", "主号"), Account("work@gmail.com", "工作")]
    a = [x.email for x in cfg.accounts]
    a2 = a[1 % len(a)]
    now = datetime.now(cfg.tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ms = db.now_ms()
    h = 3600 * 1000

    samples = [
        ("appointment", a[0], "AC 852 多伦多 → 伦敦", "明晚出发，可以开始网上值机。行李额 2 件。", "high",
         {"kind": "flight", "start": _iso(today + timedelta(days=1, hours=18, minutes=30)),
          "location": "YYZ T1", "confirmation": "Q7XK2P", "status": "confirmed"}, 5),
        ("appointment", a[0], "牙医洗牙", "Dr. Chen，记得带保险卡。", "normal",
         {"kind": "medical", "start": _iso(today + timedelta(days=4, hours=10)),
          "location": "Bay St Dental", "status": "confirmed"}, 30),
        ("appointment", a2, "Marriott London 入住", "3 晚，已含早餐。", "normal",
         {"kind": "hotel", "start": (today + timedelta(days=2)).date().isoformat(),
          "location": "London Marriott County Hall", "confirmation": "88213409"}, 50),
        ("appointment", a[0], "Alo 餐厅 2 位", "餐厅把时间改到了 20:00。", "high",
         {"kind": "restaurant", "start": _iso(today + timedelta(hours=20)), "status": "changed"}, 2),
        ("shopping", a[0], "Amazon：Anker 充电器 + USB-C 线", "今天派送中。", "high",
         {"merchant": "Amazon", "status": "out_for_delivery", "eta": today.date().isoformat(), "carrier": "Amazon"}, 1),
        ("shopping", a2, "Uniqlo：羽绒服 ×1", "已从仓库发出。", "normal",
         {"merchant": "Uniqlo", "status": "shipped", "eta": (today + timedelta(days=3)).date().isoformat(),
          "carrier": "Canada Post"}, 20),
        ("shopping", a[0], "Costco：Dyson V15", "已签收，放在前门。", "normal",
         {"merchant": "Costco", "status": "delivered"}, 26),
        ("shopping", a2, "Best Buy：AirPods Pro", "配送地址无法识别，需要更新。", "high",
         {"merchant": "Best Buy", "status": "problem", "carrier": "Purolator"}, 8),
        ("work", a2, "Q4 数据迁移方案评审", "Lisa 问周四前能不能给出迁移步骤的风险评估。", "high",
         {"person": "Lisa Wang", "needs_reply": True}, 3),
        ("work", a2, "合同条款修改", "法务改了第 7 条的赔偿上限，需要确认。", "normal",
         {"person": "Mark (Legal)", "needs_reply": True}, 28),
        ("work", a2, "周会纪要", "下周的 demo 改到周三。", "normal", {"person": "Team"}, 40),
        ("outreach", a[0], "Stripe 猎头：Staff Engineer", "支付基础设施方向，远程可谈，想约 20 分钟电话。", "normal",
         {"person": "Emily R.", "company": "Stripe", "role": "Staff Engineer"}, 12),
        ("outreach", a[0], "Jane Street 量化开发", "看到你的 GitHub，问是否考虑纽约。", "normal",
         {"person": "Recruiting", "company": "Jane Street", "role": "Quant Dev"}, 60),
        ("deal", a[0], "Amex Cobalt 开卡 15000 分", "比平时高 5000 分。", "high",
         {"merchant": "Amex", "expires": (today + timedelta(days=2)).date().isoformat(),
          "why": "开卡奖励比平时高 5000 分，周日截止"}, 9),
        ("deal", a2, "Air Canada 伦敦往返 $689", "", "normal",
         {"merchant": "Air Canada", "expires": (today + timedelta(days=6)).date().isoformat(),
          "why": "你常飞的航线，比上月低 $200"}, 33),
    ]
    for i, m in enumerate(["Lululemon", "Sephora", "Medium Daily", "Uber Eats", "Nike", "Indigo", "Airbnb", "Wayfair"]):
        samples.append(("ad", a[i % len(a)], f"{m}：本周精选", "", "normal", {"merchant": m}, 4 + i * 5))

    for n, (cat, acct, title, summary, prio, data, hours_ago) in enumerate(samples):
        ev = data.get({"appointment": "start", "shopping": "eta", "deal": "expires"}.get(cat, ""))
        db.upsert_item(conn, {
            "key": f"demo:{n}", "category": cat, "account": acct, "title": title, "summary": summary,
            "priority": prio, "data": data, "event_time": ev, "message_id": f"demo{n}",
            "thread_id": f"demo{n}", "last_date": ms - hours_ago * h,
        })
    monday = today - timedelta(days=today.weekday())
    events = [
        # Same flight Gmail already put on the calendar: should show once.
        ("AC 852 Toronto to London", today + timedelta(days=1, hours=18, minutes=30), 7.5, False, a[0], "#d8c9a7"),
        ("1:1 with Lisa", today + timedelta(hours=11), 0.5, False, a2, "#7aa6d8"),
        ("Team standup", monday + timedelta(days=7, hours=9, minutes=30), 0.25, False, a2, "#7aa6d8"),
        ("Gym", today + timedelta(days=3, hours=7), 1, False, a[0], "#9cc5a1"),
        ("妈妈生日晚饭", today + timedelta(days=5, hours=18, minutes=30), 2, False, a[0], "#e8a87c"),
        ("London trip", today + timedelta(days=2), 4, True, a[0], "#d8c9a7"),
        ("Quarterly planning", monday + timedelta(days=9, hours=13), 2, False, a2, "#7aa6d8"),
    ]
    for i, (title, start, hours, all_day, acct, color) in enumerate(events):
        conn.execute(
            """INSERT INTO events (id, account, calendar_id, calendar_name, color, title, start, end,
                                   all_day, location, url, meet_url)
               VALUES (?, ?, 'primary', ?, ?, ?, ?, ?, ?, NULL, 'https://calendar.google.com', NULL)""",
            (f"demo{i}", acct, "Work" if acct == a2 else "Personal", color, title,
             start.date().isoformat() if all_day else _iso(start),
             (start + timedelta(days=hours)).date().isoformat() if all_day else _iso(start + timedelta(hours=hours)),
             int(all_day)))
    for x in a:
        db.save_account_state(conn, x, history_id="demo")
    conn.commit()
