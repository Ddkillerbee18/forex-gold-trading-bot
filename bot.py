import os
import time
import logging
from datetime import datetime
import pandas as pd
import MetaTrader5 as mt5

from config import DEFAULT_CONFIG, TIMEFRAME_MAP
from news_filter import NewsFilter

os.makedirs(DEFAULT_CONFIG["log_dir"], exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(f"{DEFAULT_CONFIG['log_dir']}/bot.log"),
        logging.StreamHandler()
    ]
)

class MT5GoldBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.symbol = cfg["symbol"]
        self.tf = TIMEFRAME_MAP.get(cfg["timeframe"], mt5.TIMEFRAME_M5)
        self.news_filter = NewsFilter(cfg)
        self.monthly_trades = 0
        self.current_month = datetime.now().month

    def initialize(self):
        if not mt5.initialize():
            logging.error(f"MT5 Initialization failed: {mt5.last_error()}")
            return False
        
        symbol_info = mt5.symbol_info(self.symbol)
        if symbol_info is None or not symbol_info.visible:
            if not mt5.symbol_select(self.symbol, True):
                logging.error(f"Symbol {self.symbol} not available.")
                return False
        logging.info(f"Connected to MT5 successfully. Trading {self.symbol}.")
        return True

    def calculate_lot_size(self):
        account = mt5.account_info()
        if account is None:
            return self.cfg["min_lot"]

        equity = account.equity
        risk_pct = self.cfg["risk_percent_per_trade"] / 100.0
        risk_amount = equity * risk_pct

        symbol_info = mt5.symbol_info(self.symbol)
        if symbol_info is None:
            return self.cfg["min_lot"]

        point = symbol_info.point
        sl_points = self.cfg["sl_points"]
        tick_value = symbol_info.trade_tick_value

        if point == 0 or sl_points == 0 or tick_value == 0:
            return self.cfg["min_lot"]

        calculated_lot = risk_amount / (sl_points * tick_value)
        step = symbol_info.volume_step
        calculated_lot = round(calculated_lot / step) * step
        final_lot = max(self.cfg["min_lot"], min(self.cfg["max_lot"], calculated_lot))
        return round(final_lot, 2)

    def get_market_data(self):
        rates = mt5.copy_rates_from_pos(self.symbol, self.tf, 0, self.cfg["trend_window"] + 10)
        if rates is None or len(rates) < self.cfg["trend_window"]:
            return None
        
        df = pd.DataFrame(rates)
        df['sma_short'] = df['close'].rolling(self.cfg["short_window"]).mean()
        df['sma_long'] = df['close'].rolling(self.cfg["long_window"]).mean()
        df['sma_trend'] = df['close'].rolling(self.cfg["trend_window"]).mean()
        return df

    def check_signal(self, df):
        if df is None or len(df) < 2:
            return "HOLD"

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        if prev['sma_short'] <= prev['sma_long'] and curr['sma_short'] > curr['sma_long']:
            if curr['close'] > curr['sma_trend']:
                return "BUY"

        if prev['sma_short'] >= prev['sma_long'] and curr['sma_short'] < curr['sma_long']:
            if curr['close'] < curr['sma_trend']:
                return "SELL"

        return "HOLD"

    def manage_trailing_stop(self, position):
        if not self.cfg["trailing_stop_enabled"]:
            return

        symbol_info = mt5.symbol_info(self.symbol)
        if not symbol_info:
            return

        point = symbol_info.point
        dist_pts = self.cfg["trailing_stop_distance"] * point
        step_pts = self.cfg["trailing_stop_step"] * point

        if position.type == mt5.POSITION_TYPE_BUY:
            new_sl = symbol_info.bid - dist_pts
            if new_sl > position.price_open:
                if position.sl == 0 or (new_sl - position.sl) >= step_pts:
                    req = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": position.ticket,
                        "sl": round(new_sl, symbol_info.digits),
                        "tp": position.tp,
                    }
                    mt5.order_send(req)

        elif position.type == mt5.POSITION_TYPE_SELL:
            new_sl = symbol_info.ask + dist_pts
            if new_sl < position.price_open:
                if position.sl == 0 or (position.sl - new_sl) >= step_pts:
                    req = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": position.ticket,
                        "sl": round(new_sl, symbol_info.digits),
                        "tp": position.tp,
                    }
                    mt5.order_send(req)

    def execute_trade(self, signal):
        positions = mt5.positions_get(symbol=self.symbol)
        current_pos_count = len(positions) if positions else 0

        if current_pos_count >= self.cfg["max_positions"]:
            return

        now = datetime.now()
        if now.month != self.current_month:
            self.current_month = now.month
            self.monthly_trades = 0

        if self.monthly_trades >= self.cfg["monthly_trade_limit"]:
            logging.info("Monthly trade limit reached. Skipping entries.")
            return

        symbol_info = mt5.symbol_info(self.symbol)
        lot = self.calculate_lot_size()
        point = symbol_info.point

        if signal == "BUY":
            price = symbol_info.ask
            sl = price - (self.cfg["sl_points"] * point)
            tp = price + (self.cfg["tp_points"] * point)
            order_type = mt5.ORDER_TYPE_BUY
        elif signal == "SELL":
            price = symbol_info.bid
            sl = price + (self.cfg["sl_points"] * point)
            tp = price - (self.cfg["tp_points"] * point)
            order_type = mt5.ORDER_TYPE_SELL
        else:
            return

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": round(sl, symbol_info.digits),
            "tp": round(tp, symbol_info.digits),
            "deviation": self.cfg["slippage"],
            "magic": self.cfg["magic_number"],
            "comment": "MT5 Gold Bot Auto Entry",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        res = mt5.order_send(request)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            logging.info(f"Order executed successfully! Signal: {signal}, Lot: {lot}, Ticket: {res.order}")
            self.monthly_trades += 1
        else:
            logging.error(f"Order execution failed: {res.comment} (Code: {res.retcode})")

    def run(self):
        if not self.initialize():
            return

        logging.info("Trading Bot started successfully...")

        try:
            while True:
                positions = mt5.positions_get(symbol=self.symbol)
                if positions:
                    for pos in positions:
                        self.manage_trailing_stop(pos)

                if self.news_filter.is_news_impact_time():
                    logging.info("High Impact USD News detected. Trading paused.")
                    time.sleep(self.cfg["poll_interval_sec"])
                    continue

                df = self.get_market_data()
                signal = self.check_signal(df)

                if signal in ["BUY", "SELL"]:
                    self.execute_trade(signal)

                time.sleep(self.cfg["poll_interval_sec"])

        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            mt5.shutdown()

if __name__ == "__main__":
    bot = MT5GoldBot(DEFAULT_CONFIG)
    bot.run()