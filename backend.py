import os
import time
import asyncio
from decimal import Decimal, ROUND_HALF_UP
from typing import Union, Optional, List, Dict

import aiosqlite
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ---- Конфиг через env ----
YOOMONEY_ACCESS_TOKEN = os.getenv("YOOMONEY_ACCESS_TOKEN", "").strip()
YOOMONEY_RECEIVER = os.getenv("YOOMONEY_RECEIVER", "").strip()

# yoomoney — опционально (можем работать в тестовом режиме)
YOOMONEY_ENABLED = bool(YOOMONEY_ACCESS_TOKEN and YOOMONEY_RECEIVER)
if not YOOMONEY_ENABLED:
    print("[WARN] YOOMONEY_ACCESS_TOKEN или YOOMONEY_RECEIVER не заданы — депозиты будут зачисляться мгновенно (тестовый режим)")

try:
    # импортим только если нужен
    if YOOMONEY_ENABLED:
        from yoomoney import Client as YooClient
        _yoo_client = YooClient(YOOMONEY_ACCESS_TOKEN)
    else:
        _yoo_client = None
except Exception as e:
    print(f"[WARN] Не удалось инициализировать yoomoney-клиента: {e}")
    _yoo_client = None
    YOOMONEY_ENABLED = False

app = FastAPI(title="Wallet API", version="1.1.0")
DB_PATH = "/opt/wallet/wallet.db"  # абсолютный путь, чтобы не было сюрпризов

# ---- CORS ----
allowed_origins = [
    "https://kramjmka.ru",
    "https://www.kramjmka.ru",
    "http://localhost",
    "http://127.0.0.1:5173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Модели ----------
class UserIn(BaseModel):
    user_id: int = Field(..., ge=1)

class TransferIn(BaseModel):
    from_user_id: int = Field(..., ge=1)
    to_user_id: int = Field(..., ge=1)
    amount: Decimal = Field(..., gt=0)

class DepositCreateIn(BaseModel):
    user_id: int = Field(..., ge=1)
    amount: Decimal = Field(..., gt=0)

# ---------- Хелперы ----------
def dec2(value: Union[Decimal, float, int, str]) -> Decimal:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def fmt_money(value: Union[Decimal, float, int, str]) -> str:
    return f"{dec2(value):.2f}"

async def db() -> aiosqlite.Connection:
    return app.state.db

async def ensure_user(conn: aiosqlite.Connection, user_id: int) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, ?)",
        (user_id, int(time.time())),
    )
    await conn.execute(
        "INSERT OR IGNORE INTO balances(user_id, amount) VALUES (?, 0)",
        (user_id,),
    )
    await conn.execute(
        "INSERT OR IGNORE INTO admin_users(user_id, admin) VALUES (?, 0)",
        (user_id,),
    )

async def start_immediate_tx(conn: aiosqlite.Connection) -> None:
    """
    SQLite иногда ругается 'cannot start a transaction within a transaction'.
    Подстрахуемся: откатим незавершённую транзакцию и начнём новую IMMEDIATE.
    """
    try:
        in_tx = getattr(conn, "in_transaction", False)
    except Exception:
        in_tx = False

    if in_tx:
        try:
            await conn.rollback()
        except Exception:
            pass

    await conn.execute("BEGIN IMMEDIATE;")

# ---------- Жизненный цикл ----------
@app.on_event("startup")
async def startup():
    app.state.db = await aiosqlite.connect(DB_PATH)
    conn = await db()
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA synchronous=NORMAL;")

    # Таблицы
    await conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id    INTEGER PRIMARY KEY,
        created_at INTEGER NOT NULL
    );
    """)
    await conn.execute("""
    CREATE TABLE IF NOT EXISTS balances (
        user_id INTEGER PRIMARY KEY,
        amount  REAL NOT NULL DEFAULT 0
    );
    """)
    await conn.execute("""
    CREATE TABLE IF NOT EXISTS tx (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user INTEGER NOT NULL,
        to_user   INTEGER NOT NULL,
        amount    REAL NOT NULL,
        ts        INTEGER NOT NULL
    );
    """)
    await conn.execute("""
    CREATE TABLE IF NOT EXISTS admin_users (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        admin   INTEGER NOT NULL DEFAULT 0
    );
    """)
    # Депозиты
    await conn.execute("""
    CREATE TABLE IF NOT EXISTS deposits (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        amount     REAL    NOT NULL,
        label      TEXT    NOT NULL UNIQUE,
        status     TEXT    NOT NULL,               -- pending|done|failed
        created_ts INTEGER NOT NULL,
        done_ts    INTEGER
    );
    """)
    await conn.commit()

    # Фоновый воркер депозита (если YoоMoney доступен)
    app.state.deposit_task: Optional[asyncio.Task] = None
    if YOOMONEY_ENABLED and _yoo_client is not None:
        app.state.deposit_task = asyncio.create_task(deposits_checker())

@app.on_event("shutdown")
async def shutdown():
    # Аккуратно завершаем фоновую задачу
    task = getattr(app.state, "deposit_task", None)
    if isinstance(task, asyncio.Task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await app.state.db.close()

# ---------- Служебное ----------
@app.get("/ping")
async def ping_root():
    return {"ok": True}

@app.get("/api/ping")
async def ping_api():
    return {"ok": True}

# ---------- Эндпоинты для бота ----------
@app.post("/users")
async def create_user(body: UserIn):
    conn = await db()
    await ensure_user(conn, body.user_id)
    await conn.commit()
    return {"ok": True}

@app.get("/users")
async def list_users():
    conn = await db()
    cur = await conn.execute("SELECT user_id FROM users ORDER BY user_id")
    rows = await cur.fetchall()
    return {"user_ids": [r[0] for r in rows]}

@app.get("/admins/{user_id}")
async def is_admin(user_id: int):
    conn = await db()
    cur = await conn.execute("SELECT admin FROM admin_users WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    return {"is_admin": bool(row and row[0] == 1)}

@app.post("/admins/{user_id}")
async def make_admin(user_id: int):
    conn = await db()
    await ensure_user(conn, user_id)
    await conn.execute("UPDATE admin_users SET admin = 1 WHERE user_id = ?", (user_id,))
    await conn.commit()
    return {"ok": True}

# ---------- Эндпоинты мини-аппа ----------
@app.get("/api/me")
async def me(user_id: int = Query(..., ge=1)):
    conn = await db()
    await ensure_user(conn, user_id)
    cur = await conn.execute("SELECT amount FROM balances WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    balance = dec2(row[0] if row else 0)
    return {"user_id": user_id, "balance": fmt_money(balance)}

@app.get("/api/history")
async def history(
    user_id: int = Query(..., ge=1),
    limit: int = Query(50, ge=1, le=200)
):
    conn = await db()
    await ensure_user(conn, user_id)
    cur = await conn.execute(
        """
        SELECT from_user, to_user, amount, ts
        FROM tx
        WHERE from_user = ? OR to_user = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (user_id, user_id, limit),
    )
    items: List[Dict[str, Union[str, float, int]]] = []
    rows = await cur.fetchall()
    for fuid, tuid, amount, ts in rows:
        amount_dec = dec2(amount)
        signed = amount_dec if tuid == user_id else dec2(-amount_dec)
        title = ("От %d" % fuid) if tuid == user_id else ("К %d" % tuid)
        items.append({"title": title, "amount": float(signed), "ts": ts * 1000})
    return {"items": items}

@app.post("/api/transfer")
async def transfer(body: TransferIn):
    if body.from_user_id == body.to_user_id:
        raise HTTPException(400, "Перевод самому себе не имеет смысла")

    amount = dec2(body.amount)
    if amount <= 0:
        raise HTTPException(400, "Сумма должна быть больше нуля")

    now = int(time.time())
    conn = await db()
    await ensure_user(conn, body.from_user_id)
    await ensure_user(conn, body.to_user_id)

    # Транзакция с защитой
    await start_immediate_tx(conn)
    try:
        # баланс отправителя
        cur = await conn.execute("SELECT amount FROM balances WHERE user_id = ?", (body.from_user_id,))
        row = await cur.fetchone()
        from_balance = dec2(row[0] if row else 0)
        if from_balance < amount:
            await conn.rollback()
            raise HTTPException(400, "Недостаточно средств")

        # списание/зачисление
        await conn.execute(
            "UPDATE balances SET amount = amount - ? WHERE user_id = ?",
            (float(amount), body.from_user_id),
        )
        await conn.execute(
            "UPDATE balances SET amount = amount + ? WHERE user_id = ?",
            (float(amount), body.to_user_id),
        )
        await conn.execute(
            "INSERT INTO tx(from_user, to_user, amount, ts) VALUES (?,?,?,?)",
            (body.from_user_id, body.to_user_id, float(amount), now),
        )

        # новый баланс отправителя
        cur2 = await conn.execute("SELECT amount FROM balances WHERE user_id = ?", (body.from_user_id,))
        new_from_balance = dec2((await cur2.fetchone())[0])
        await conn.commit()
        return {"ok": True, "from_balance": fmt_money(new_from_balance)}
    except:
        try:
            await conn.rollback()
        except Exception:
            pass
        raise

# ---------- Депозиты (YooMoney) ----------
@app.post("/api/deposit/create")
async def deposit_create(body: DepositCreateIn):
    """
    Создаём заявку на пополнение. Если YooMoney настроен — вернём redirect_url.
    Если нет — тестовый режим: сразу зачисляем.
    """
    amount = dec2(body.amount)
    if amount <= 0:
        raise HTTPException(400, "Сумма должна быть больше нуля")

    conn = await db()
    await ensure_user(conn, body.user_id)
    label = f"dep:{body.user_id}:{int(time.time())}"

    now = int(time.time())

    if not YOOMONEY_ENABLED or _yoo_client is None:
        # Тестовый режим — мгновенное пополнение
        await start_immediate_tx(conn)
        try:
            await conn.execute(
                "UPDATE balances SET amount = amount + ? WHERE user_id = ?",
                (float(amount), body.user_id),
            )
            await conn.execute(
                "INSERT INTO tx(from_user, to_user, amount, ts) VALUES (?,?,?,?)",
                (0, body.user_id, float(amount), now),
            )
            await conn.execute(
                "INSERT INTO deposits(user_id, amount, label, status, created_ts, done_ts) VALUES (?,?,?,?,?,?)",
                (body.user_id, float(amount), label, "done", now, now),
            )
            await conn.commit()
            return {
                "ok": True,
                "mode": "test",
                "label": label,
                "redirect_url": None
            }
        except:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise

    # Боевой режим — формируем URL для оплаты через quickpay
    # Документация quickpay (YooMoney/Деньги) — классическая форма:
    # https://yoomoney.ru/transfer
    # Ниже отдаём минимально необходимый URL; фронт может открыть его в webview.
    from urllib.parse import urlencode

    params = {
        "receiver": YOOMONEY_RECEIVER,
        "quickpay-form": "donate",
        "sum": fmt_money(amount),
        "label": label,
        "targets": f"Пополнение {body.user_id}",
        "paymentType": "AC",  # Банковская карта (как пример)
        "successURL": "https://kramjmka.ru/?success=1"
    }
    redirect_url = "https://yoomoney.ru/quickpay/confirm.xml?" + urlencode(params)

    # Запишем pending
    await conn.execute(
        "INSERT INTO deposits(user_id, amount, label, status, created_ts, done_ts) VALUES (?,?,?,?,?,NULL)",
        (body.user_id, float(amount), label, "pending", now),
    )
    await conn.commit()

    return {
        "ok": True,
        "mode": "yoomoney",
        "label": label,
        "redirect_url": redirect_url
    }

async def deposits_checker():
    """
    Фоновая задача: каждые N секунд проверяет невыполненные депозиты в YooMoney.
    При успехе — зачисляет средства.
    """
    if not YOOMONEY_ENABLED or _yoo_client is None:
        return  # на всякий случай

    poll_interval = 10  # сек
    while True:
        try:
            conn = await db()
            cur = await conn.execute(
                "SELECT id, user_id, amount, label FROM deposits WHERE status = 'pending' ORDER BY id ASC LIMIT 50"
            )
            rows = await cur.fetchall()
            if rows:
                # Для каждой заявки проверим историю операций по label
                for dep_id, user_id, amount, label in rows:
                    try:
                        history = _yoo_client.operation_history(label=label)
                    except Exception as e:
                        # Не упадём целиком из-за одного платежа
                        print(f"[deposits_checker] Ошибка history(label={label}): {e}")
                        continue

                    ok = False
                    try:
                        for op in getattr(history, "operations", []) or []:
                            if getattr(op, "status", "") == "success":
                                ok = True
                                break
                    except Exception:
                        pass

                    if ok:
                        # зачисляем
                        await start_immediate_tx(conn)
                        try:
                            now = int(time.time())
                            await conn.execute(
                                "UPDATE balances SET amount = amount + ? WHERE user_id = ?",
                                (float(amount), user_id),
                            )
                            await conn.execute(
                                "INSERT INTO tx(from_user, to_user, amount, ts) VALUES (?,?,?,?)",
                                (0, user_id, float(amount), now),
                            )
                            await conn.execute(
                                "UPDATE deposits SET status='done', done_ts=? WHERE id=?",
                                (now, dep_id),
                            )
                            await conn.commit()
                        except Exception as e:
                            print(f"[deposits_checker] Ошибка зачисления dep_id={dep_id}: {e}")
                            try:
                                await conn.rollback()
                            except Exception:
                                pass
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[deposits_checker] loop error: {e}")
            await asyncio.sleep(poll_interval)
