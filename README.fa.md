# Stop Order Scalp

[English version](README.md)

**سیستم اسکالپینگ سفارش‌استاپ روی US30 — فیلتر جهت M15، ورود با استاپ M1، اجرا روی MT5.**

> **وضعیت: در حال توسعه.** برای دیدن وضعیت فعلی به [`ROADMAP.md`](ROADMAP.md) و
> [`HANDOFF.md`](HANDOFF.md) مراجعه کنید.
>
> **فاز ۱ (پایه‌گذاری پروژه) کامل شده است.** لایه دامنه، پیکربندی، لاگ، ساعت، تایم‌فریم‌ها و
> دروازه معماری پیاده‌سازی و تست شده‌اند — ۲۷۴ تست، بدون نیاز به بروکر. از هفت فرمان خط
> فرمان، **فقط `validate-config` کار می‌کند**؛ شش فرمان دیگر با کد **۴** خارج می‌شوند و
> اعلام می‌کنند کدام فاز هنوز ساخته نشده است. سطح فرمان‌ها عمداً پیش از پیاده‌سازی‌هایش
> تثبیت شده تا قرارداد همین حالا قابل تست باشد.
>
> **هیچ ادعایی درباره سودآوری وجود ندارد.** سودآوری تا زمانی که آزمون آماری معنادار
> انجام نشود، قابل اعلام نیست.
>
> **هشدار ریسک.** معاملات خودکار ریسک زیان قابل توجهی دارد. ابتدا در حالت
> `DRY_RUN` و سپس `PAPER` و در نهایت روی حساب دمو آزمایش کنید. حالت `LIVE` هنوز
> پیاده‌سازی نشده و سه بار قفل شده است؛ پیش از نزدیک شدن به آن
> [`docs/operations/`](docs/operations/) را بخوانید.

---

## هدف

یک برنامه معاملاتی ماژولار، تست‌پذیر و شیءگرا که **دقیقاً یک استراتژی** را روی **یک
نماد** پیاده‌سازی می‌کند:

* معامله **فقط روی US30**.
* کندل **۱۵ دقیقه‌ای** جهت مجاز را تعیین می‌کند.
* سقف/کف کندل **۱ دقیقه‌ای** یک **سفارش استاپ معلق** با فاصله ۱۰ پوینت قرار می‌دهد.
* حجم معامله بر اساس **۰٫۵٪ از موجودی حساب** و با در نظر گرفتن کمیسیون محاسبه
  می‌شود.
* **سر به‌سر** در نقطه فعال‌سازی قابل تنظیم، سپس **تریلینگ‌استاپ ۱۰۰ پوینتی** که هرگز
  عقب‌گرد نمی‌کند.
* به‌محض بسته شدن معامله، **بلافاصله** سفارش معلق بعدی ثبت می‌شود.

تمام پارامترهای استراتژی در پیکربندی هستند. هیچ چیزی از متاتریدر ۵ به لایه استراتژی،
ریسک یا چرخه‌عمر نشت نمی‌کند.

## نقشه مستندات

| سند | کاربرد |
| --- | --- |
| [`ROADMAP.md`](ROADMAP.md) | نقشه راه فازبندی‌شده و وضعیت هر فاز |
| [`HANDOFF.md`](HANDOFF.md) | فایل اجباری وضعیت — **اول این را بخوانید** |
| [`CHANGELOG.md`](CHANGELOG.md) | تاریخچه تغییرات |
| [`docs/architecture/`](docs/architecture/) | معماری، تصمیم‌ها، ممیزی |
| [`docs/strategy/BASELINE.md`](docs/strategy/BASELINE.md) | قواعد دقیق استراتژی |
| [`docs/risk/`](docs/risk/) | حجم‌دهی، کمیسیون، معنی SL/TP |
| [`docs/mt5/`](docs/mt5/) | راه‌اندازی MT5، مشخصات نماد، حالت خشک |
| [`docs/testing/`](docs/testing/) | ساختار تست‌ها، تست ویژگی‌ها، اجرا |
| [`docs/operations/`](docs/operations/) | استقرار، عیب‌یابی، بازیابی |
| [`docs/research/`](docs/research/) | لایه پژوهشی اختیاری و به‌طور پیش‌فرض غیرفعال |

## شروع سریع

```bash
git clone git@github.com:ybagheri/stop-order-scalp.git
cd stop-order-scalp
python -m venv .venv
# ویندوز: .venv\Scripts\activate   |   لینوکس/مک: source .venv/bin/activate
pip install -e ".[dev]"
copy .env.example .env
python -m stop_order_scalp validate-config
python -m stop_order_scalp test-connection
python -m stop_order_scalp run --dry-run
```

## خط فرمان

```bash
python -m stop_order_scalp run                # معاملات واقعی (فقط با فعال‌سازی صریح)
python -m stop_order_scalp run --dry-run      # کل فرایند، بدون هیچ نوشتنی روی بروکر
python -m stop_order_scalp status             # اتصال، حساب، نماد، وضعیت، ریسک
python -m stop_order_scalp validate-config    # اعتبارسنجی پیکربندی
python -m stop_order_scalp backtest --help    # بازپخش داده تاریخی
python -m stop_order_scalp test-connection    # بررسی دسترسی MT5، فقط خواندنی
python -m stop_order_scalp journal            # ژورنال معاملات
python -m stop_order_scalp diagnostics        # بسته محیط و پیکربندی
```

حالت‌های اجرا (`SOS_ENVIRONMENT`): `DRY_RUN` (پیش‌فرض) → `PAPER` → `DEMO` → `LIVE`.
هر عملیات تغییردهنده حساب **کلید فعال‌سازی مستقل** خودش را دارد و `LIVE` علاوه بر آن
به `SOS_ALLOW_LIVE=true` نیاز دارد. هیچ مسیری به‌طور تصادفی به معاملات واقعی نمی‌رسد.

### کدهای خروج

بخشی از قرارداد هستند، چون یک فرایند ناظر به آن‌ها وابسته است.

| کد | معنی |
| --- | --- |
| 0 | موفقیت |
| 1 | اجرا شد و شکست خورد (رد بروکر، رد سفارش) |
| 2 | پیش از هر کاری رد شد (پیکربندی نامعتبر، دروازه ایمنی) |
| 3 | به متاتریدر ۵ متصل نیست |
| 4 | فرمان وجود دارد، ولی فاز پیاده‌سازی آن هنوز ساخته نشده |

## توسعه

```bash
python -m pytest                             # ۲۷۴ تست
python -m ruff check .                       # لینت
python -m mypy                               # نوع‌ها، حالت strict، src و tests
python scripts/check_architecture.py         # مرزهای معماری
```

دستور `pytest` هر چهار دروازه را پوشش می‌دهد، اما هنگام کار آن‌ها را جداگانه اجرا کنید تا
خروجی خوانا باشد. [`docs/testing/`](docs/testing/) و [`CONTRIBUTING.md`](CONTRIBUTING.md)
را ببینید.

## پیکربندی

پارامترهای استراتژی: [`config/default.yaml`](config/default.yaml).
مقادیر مخصوص هر ماشین (مسیر MT5، شماره حساب، سرور، عدد جادویی، حالت اجرا): فایل `.env`
(نمونه در [`.env.example`](.env.example)).

```yaml
symbol: US30
symbol_aliases: [US30, US30.cash, US30m, DJ30]   # تطبیق دقیق و بدون حساسیت به بزرگی/کوچکی حروف

entry:
  timeframe: M1
  direction_timeframe: M15
  offset_points: 10                            # BUY STOP = high+10, SELL STOP = low-10
  candle_selection: last_closed                # هرگز کندل در حال تشکیل: بدون نگاه به آینده

risk:
  mode: percent_balance                         # یا: fixed_lot
  percent: 0.5
  fixed_lot: 0.10
  commission_per_lot: 6.0
  commission_mode: per_lot_round_trip

target:
  mode: fixed_points                            # یا: risk_reward
  take_profit_points: 1000
  risk_reward: 1.0
  stop_loss_points: 100                         # فاصله از ورود، وقتی حالتی به SL نیاز دارد

break_even:
  enabled: true
  trigger_points: 50
  mode: entry                                   # یا: commission_aware

trailing:
  enabled: true
  distance_points: 100
  min_step_points: 1                            # هر تیک درخواست تغییر نفرست
```

کلید ناشناخته در هر بخشی از این فایل یک **خطای قطعی همراه با نام بخش** است، نه یک هشدار.
غلط املایی در پارامتر ریسک که بی‌صدا نادیده گرفته شود، گران‌ترین نوع باگ پیکربندی است.

## معماری

```
CLI  →  لایه کاربرد (TradingService)  →  ارکستراتور
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    ▼                     ▼                     ▼
               استراتژی              موتور ریسک              چرخه‌عمر
          (جهت M15، قواعد          (حجم، کمیسیون،         (ماشین حالت،
           ورود استاپ M1)            SL/TP، اعتبارسنجی)      جایگزینی)
                    │                     │                     │
                    └─────────────────────┴─────────────────────┘
                                          ▼
                                   لایه اجرا  ──►  بروکر (Protocol)
                                          │
                       ┌──────────────────┴──────────────────┐
                       ▼                                     ▼
              MetaTrader5Broker                       SimulatedBroker
                 (رابط بومی)                        (خشک / کاغذی / بک‌تست)
```

* لایه **دامنه** مستقل از متاتریدر ۵، بدون عدد اعشاری برای پول، و بدون I/O است.
* `strategy`، `risk`، `trailing` و `lifecycle` هرگز `MetaTrader5` را import
  نمی‌کنند. اسکریپت `scripts/check_architecture.py` این موضوع را در CI اجباری می‌کند.
* `simulated_broker` یک پیاده‌سازی درجه‌یک است، نه یک دوبل تست — چیزی است که حالت
  `DRY_RUN` و بک‌تستر روی آن اجرا می‌شوند.

جزئیات: [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md).

## مشارکت

[`CONTRIBUTING.md`](CONTRIBUTING.md) را ببینید. هر فاز باید با این موارد تمام شود:
تست‌ها سبز، مستندات به‌روز، `ROADMAP.md` / `HANDOFF.md` / `CHANGELOG.md` بازنگری‌شده،
درخت کاری گیت تمیز، کامیت ساخته‌شده و push موفق.

## امنیت

* هیچ اطلاعات محرمانه‌ای در گیت نیست. فایل `.env` نادیده گرفته می‌شود.
* لاگ‌ها ساختاریافته‌اند: فیلد محرمانه در مدل رکورد لاگ وجود ندارد، پس توکن
  به‌اشتباه لاگ نمی‌شود.
* حالت `LIVE` به دو فعال‌سازی مستقل به‌علاوه قفل دمو نیاز دارد.

## مجوز

اختصاصی. [`LICENSE`](LICENSE) را ببینید.

## سلب مسئولیت

صرفاً برای **پژوهش و آموزش** ارائه شده است. این نرم‌افزار مشاوره مالی، سرمایه‌گذاری یا
معامله نیست. معاملات اهرمی می‌توانند بیش از میزان سپرده شما را از دست بدهند.