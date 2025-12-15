import logging
import sqlite3


class BotDatabase:
    def __init__(self, db_path="bot_data.db"):
        self.db_path = db_path
        self.logger = logging.getLogger(self.__class__.__name__)
        self._init_db()

    def _init_db(self):
        """Initialize the orders table if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # We enforce a UNIQUE constraint on (bot_id, price, status)
        # so we physically cannot duplicate an open order at the same price.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                order_id TEXT,
                price REAL,
                side TEXT,
                quantity REAL,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Index for fast lookups
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bot_price_status ON grid_orders (bot_id, price, status)")
        conn.commit()
        conn.close()

    def add_order(self, bot_id: int, order_id: str, price: float, side: str, quantity: float):
        """Saves a new active order to the DB."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO grid_orders (bot_id, order_id, price, side, quantity, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (bot_id, order_id, price, side, quantity, "OPEN"),
            )
            conn.commit()
            self.logger.info(f"💾 DB: Saved {side} order {order_id} at {price}")
        except Exception as e:
            self.logger.error(f"Failed to save order to DB: {e}")
        finally:
            conn.close()

    def update_order_status(self, order_id: str, new_status: str):
        """Updates an order status (e.g. OPEN -> CLOSED)."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE grid_orders 
            SET status = ?, updated_at = CURRENT_TIMESTAMP 
            WHERE order_id = ?
        """,
            (new_status, order_id),
        )
        conn.commit()
        conn.close()

    def get_active_order_at_price(self, bot_id: int, price: float, tolerance: float = 0.001):
        """
        Checks if we ALREADY have an open order at this price.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get all open orders for this bot
        cursor.execute('SELECT order_id, price, side FROM grid_orders WHERE bot_id = ? AND status = "OPEN"', (bot_id,))
        rows = cursor.fetchall()
        conn.close()

        # Check with tolerance (handling floating point math)
        for row in rows:
            db_order_id, db_price, db_side = row
            if abs(db_price - price) < tolerance:
                return {"order_id": db_order_id, "price": db_price, "side": db_side}

        return None

    def get_all_active_orders(self, bot_id: int):
        """Returns map of active orders for initialization."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT order_id, price, side, quantity FROM grid_orders WHERE bot_id = ? AND status = "OPEN"', (bot_id,)
        )
        rows = cursor.fetchall()
        conn.close()

        # Return dict keyed by Order ID
        return {row[0]: {"price": row[1], "side": row[2], "amount": row[3]} for row in rows}

    def clear_all_orders(self, bot_id: int):
        """Deletes ALL open orders for a specific bot (Clean Start)."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM grid_orders WHERE bot_id = ?", (bot_id,))
            conn.commit()
            self.logger.info(f"🧹 DB: Cleared all orders for Bot {bot_id}")
        except Exception as e:
            self.logger.error(f"Failed to clear DB orders: {e}")
        finally:
            conn.close()
