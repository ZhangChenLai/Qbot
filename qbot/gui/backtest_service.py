"""Data retrieval and portfolio simulation for the visual backtest panel."""

from __future__ import annotations

import base64
import io
import math
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd


_DATE_FORMAT = "%Y%m%d"
_INDEX_CODES = {"000001", "000016", "000300", "000905", "000852", "399001", "399006"}
_EASTMONEY_INDEX_CODES = {
    "000001": "sh000001",
    "000016": "sh000016",
    "000300": "sh000300",
    "000905": "sh000905",
    "000852": "sh000852",
    "399001": "sz399001",
    "399006": "sz399006",
}


def _normalize_bars(frame: pd.DataFrame, date_column: str, close_column: str) -> pd.DataFrame:
    """Convert a data-provider response to sorted, numeric OHLCV bars."""
    if frame is None or frame.empty:
        raise ValueError("数据源未返回行情数据，请检查代码、日期范围或网络连接。")

    bars = frame.copy()
    rename = {
        date_column: "date",
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        close_column: "close",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    }
    bars = bars.rename(columns={key: value for key, value in rename.items() if key in bars})
    if "date" not in bars or "close" not in bars:
        raise ValueError("行情数据缺少日期或收盘价字段。")

    bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in bars:
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
    bars = bars.dropna(subset=["date", "close"]).sort_values("date")
    bars = bars.drop_duplicates(subset=["date"], keep="last").set_index("date")
    if bars.empty:
        raise ValueError("指定日期范围内没有有效行情数据。")
    return bars


def fetch_market_data(
    code: str,
    start_time: str,
    end_time: str,
    *,
    is_benchmark: bool = False,
    period: str = "daily",
    adjust: str = "qfq",
    asset_type: str = "stock",
) -> pd.DataFrame:
    """Fetch daily/weekly Chinese stock, index, ETF, or open-fund history via AKShare."""
    import akshare as ak

    start = pd.to_datetime(start_time, format=_DATE_FORMAT).strftime(_DATE_FORMAT)
    end = pd.to_datetime(end_time, format=_DATE_FORMAT).strftime(_DATE_FORMAT)
    if start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    if period not in {"daily", "weekly"}:
        raise ValueError("当前数据源支持日线和周线，暂不支持分钟线回测。")

    raw_code = code.strip().upper()
    symbol = raw_code.split(".", 1)[0]
    if not symbol:
        raise ValueError("请输入股票、指数或基金代码。")

    is_index = (
        is_benchmark
        or symbol.startswith("399")
        or (symbol in _INDEX_CODES and raw_code.endswith((".SH", ".SZ")))
    )
    if is_index:
        primary_error = None
        try:
            frame = ak.stock_zh_index_daily_em(
                symbol=_EASTMONEY_INDEX_CODES.get(symbol, symbol),
                start_date=start,
                end_date=end,
            )
            bars = _normalize_bars(frame, "日期", "收盘")
        except Exception as exc:
            primary_error = exc
            sina_symbol = f"{'sz' if symbol.startswith('399') else 'sh'}{symbol}"
            try:
                # Sina uses an independent endpoint and can still serve data when
                # Eastmoney's push2 history endpoint is blocked or unavailable.
                frame = ak.stock_zh_index_daily(symbol=sina_symbol)
                bars = _normalize_bars(frame, "date", "close")
            except Exception as fallback_error:
                raise RuntimeError(
                    f"获取指数 {symbol} 行情失败：东方财富和新浪数据源均不可用，"
                    f"请检查网络/代理后重试。（东方财富：{primary_error}；新浪：{fallback_error}）"
                ) from fallback_error

        bars = bars.loc[pd.to_datetime(start) : pd.to_datetime(end)]
        if period == "weekly" and not bars.empty:
            aggregation = {"close": "last"}
            for column in ("open", "high", "low"):
                if column in bars:
                    aggregation[column] = "first" if column == "open" else (
                        "max" if column == "high" else "min"
                    )
            for column in ("volume", "amount"):
                if column in bars:
                    aggregation[column] = "sum"
            bars = bars.resample("W-FRI").agg(aggregation).dropna(subset=["close"])
        if bars.empty:
            raise ValueError(f"指数 {symbol} 在所选日期范围内没有行情数据。")
        return bars

    if not symbol.isdigit() or len(symbol) != 6:
        raise ValueError("代码格式不正确，请输入 6 位 A 股/指数/基金代码。")

    if asset_type == "etf":
        if period != "daily":
            raise ValueError("ETF 暂只支持日线回测。")
        try:
            frame = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start,
                end_date=end,
                adjust=adjust,
            )
        except Exception as exc:
            raise RuntimeError(
                f"获取 ETF {symbol} 行情失败，请检查网络连接或稍后重试。"
            ) from exc
        return _normalize_bars(frame, "日期", "收盘")

    if asset_type not in {"stock", "fund"}:
        raise ValueError("不支持的标的类型，请选择 A 股/指数、开放式基金或 ETF。")
    if asset_type == "stock":
        try:
            frame = ak.stock_zh_a_hist(
                symbol=symbol,
                period=period,
                start_date=start,
                end_date=end,
                adjust=adjust,
            )
        except Exception as exc:
            raise RuntimeError(
                f"获取股票 {symbol} 行情失败，请检查网络连接或稍后重试。"
            ) from exc
        return _normalize_bars(frame, "日期", "收盘")

    if period != "daily":
        raise ValueError("开放式基金净值仅支持日线回测。")
    try:
        frame = ak.fund_open_fund_info_em(symbol=symbol, indicator="单位净值走势")
    except Exception as exc:
        raise RuntimeError(
            f"获取基金 {symbol} 净值失败，请检查网络连接或稍后重试。"
        ) from exc
    if frame is None or frame.empty:
        raise ValueError("基金净值数据为空，请确认基金代码有效。")
    date_col = next((c for c in frame if "净值日期" in str(c)), None)
    value_col = next((c for c in frame if "单位净值" in str(c)), None)
    if date_col is None or value_col is None:
        raise ValueError("基金净值数据格式无法识别。")
    frame = frame.rename(columns={date_col: "date", value_col: "close"})
    bars = _normalize_bars(frame, "date", "close")
    return bars.loc[pd.to_datetime(start) : pd.to_datetime(end)]


def _strategy_position(close: pd.Series, strategy_name: str) -> pd.Series:
    """Create a long/flat position series for the supported GUI strategies."""
    name = strategy_name.lower()
    price = close.astype(float)

    if "rsi" in name or not strategy_name:
        delta = price.diff()
        average_gain = delta.clip(lower=0).ewm(alpha=1 / 21, min_periods=21, adjust=False).mean()
        average_loss = -delta.clip(upper=0).ewm(alpha=1 / 21, min_periods=21, adjust=False).mean()
        relative_strength = average_gain / average_loss.replace(0, float("nan"))
        rsi = 100 - 100 / (1 + relative_strength)
        rsi = rsi.mask((average_loss == 0) & (average_gain > 0), 100)
        rsi = rsi.mask((average_loss == 0) & (average_gain == 0), 50)
        signal = pd.Series(float("nan"), index=price.index)
        signal.loc[rsi < 40] = 1.0
        signal.loc[rsi > 65] = 0.0
        return signal.ffill().fillna(0.0)

    if "均线" in strategy_name or "移动平均" in strategy_name or "ma" in name:
        fast = price.rolling(20, min_periods=20).mean()
        slow = price.rolling(60, min_periods=60).mean()
        return (fast > slow).astype(float).where(slow.notna(), 0.0)

    if "布林" in strategy_name or "boll" in name:
        mean = price.rolling(20, min_periods=20).mean()
        deviation = price.rolling(20, min_periods=20).std()
        signal = pd.Series(float("nan"), index=price.index)
        signal.loc[price < mean - 2 * deviation] = 1.0
        signal.loc[price > mean] = 0.0
        return signal.ffill().fillna(0.0)

    if "macd" in name or "macd" in strategy_name:
        difference = price.ewm(span=12, adjust=False).mean() - price.ewm(span=26, adjust=False).mean()
        signal_line = difference.ewm(span=9, adjust=False).mean()
        return (difference > signal_line).astype(float)

    raise ValueError(f"策略“{strategy_name}”尚未实现，请选择 RSI、均线或布林线策略。")


def run_backtest(
    bars: pd.DataFrame,
    benchmark: pd.DataFrame,
    *,
    strategy_name: str,
    initial_cash: float,
    stake: int,
    commission: float,
    stamp_duty: float,
    slippage_percent: float = 0.1,
) -> dict[str, Any]:
    """Run a long/flat, fixed-share strategy and return portfolio/report series."""
    if bars is None or bars.empty:
        raise ValueError("请先加载有效行情数据。")
    if initial_cash <= 0 or stake <= 0:
        raise ValueError("初始资金和交易规模必须大于 0。")
    if min(commission, stamp_duty, slippage_percent) < 0:
        raise ValueError("手续费、印花税和滑点不能为负数。")

    data = bars.copy().sort_index()
    prices = pd.to_numeric(data["close"], errors="coerce").dropna()
    if len(prices) < 2:
        raise ValueError("有效行情至少需要两个交易日。")
    positions = _strategy_position(prices, strategy_name)

    cash = float(initial_cash)
    shares = 0
    portfolio_values: list[float] = []
    trades: list[dict[str, Any]] = []
    slip = slippage_percent / 100.0
    for date, close in prices.items():
        desired_shares = int(stake) if positions.loc[date] > 0 else 0
        if desired_shares > shares:
            quantity = desired_shares - shares
            execution_price = float(close) * (1 + slip)
            affordable = int(cash / (execution_price * (1 + commission)))
            quantity = min(quantity, affordable)
            if quantity > 0:
                fee = quantity * execution_price * commission
                cash -= quantity * execution_price + fee
                shares += quantity
                trades.append({"date": date, "side": "买入", "price": execution_price, "quantity": quantity, "fee": fee})
        elif desired_shares < shares:
            quantity = shares - desired_shares
            execution_price = float(close) * (1 - slip)
            fee = quantity * execution_price * (commission + stamp_duty)
            cash += quantity * execution_price - fee
            shares -= quantity
            trades.append({"date": date, "side": "卖出", "price": execution_price, "quantity": quantity, "fee": fee})
        portfolio_values.append(cash + shares * float(close))

    result = pd.DataFrame({"equity": portfolio_values}, index=prices.index)
    result["strategy_return"] = result["equity"].pct_change().fillna(0.0)
    base_close = pd.to_numeric(benchmark["close"], errors="coerce").dropna()
    if len(prices) > 1 and (prices.index.to_series().diff().dropna().median().days >= 5):
        base_close = base_close.resample("W-FRI").last().dropna()
    aligned = pd.concat([result["equity"], base_close.rename("benchmark")], axis=1).dropna()
    if aligned.empty:
        raise ValueError("基准数据与回测区间没有重叠交易日，请更换基准或日期。")
    result = result.loc[aligned.index]
    result["strategy_return"] = result["equity"].pct_change().fillna(0.0)
    result["strategy"] = result["equity"] / float(initial_cash)
    result["benchmark"] = aligned["benchmark"] / float(aligned["benchmark"].iloc[0])

    total_return = float(result["strategy"].iloc[-1] - 1)
    benchmark_return = float(result["benchmark"].iloc[-1] - 1)
    days = max((result.index[-1] - result.index[0]).days, 1)
    annual_return = float(result["strategy"].iloc[-1] ** (365.25 / days) - 1)
    running_peak = result["equity"].cummax()
    max_drawdown = float((result["equity"] / running_peak - 1).min())
    volatility = float(result["strategy_return"].std() * math.sqrt(252))
    sharpe = float(result["strategy_return"].mean() / result["strategy_return"].std() * math.sqrt(252)) if result["strategy_return"].std() else 0.0
    return {
        "series": result,
        "trades": trades,
        "initial_cash": float(initial_cash),
        "final_value": float(result["equity"].iloc[-1]),
        "total_return": total_return,
        "annual_return": annual_return,
        "benchmark_return": benchmark_return,
        "max_drawdown": max_drawdown,
        "volatility": volatility,
        "sharpe": sharpe,
    }


def write_report(result: dict[str, Any], output_file: Path, *, code: str, benchmark_code: str, strategy_name: str) -> Path:
    """Write a self-contained HTML report that can be rendered by wx.html2."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    series = result["series"]
    figure, axis = plt.subplots(figsize=(12, 5))
    dates = series.index.to_pydatetime()
    axis.plot(
        dates,
        ((series["strategy"] - 1) * 100).to_numpy(),
        label="Strategy",
    )
    axis.plot(
        dates,
        ((series["benchmark"] - 1) * 100).to_numpy(),
        label="Benchmark",
    )
    axis.set_title(f"Backtest Returns: {escape(code)}")
    axis.set_ylabel("Cumulative return (%)")
    axis.grid(True, alpha=0.3)
    axis.legend()
    figure.tight_layout()
    image_buffer = io.BytesIO()
    figure.savefig(image_buffer, format="png", dpi=140)
    plt.close(figure)
    chart = base64.b64encode(image_buffer.getvalue()).decode("ascii")

    metrics = [
        ("期初资金", f"{result['initial_cash']:,.2f}"),
        ("期末资产", f"{result['final_value']:,.2f}"),
        ("策略总收益", f"{result['total_return']:.2%}"),
        ("年化收益", f"{result['annual_return']:.2%}"),
        ("基准收益", f"{result['benchmark_return']:.2%}"),
        ("最大回撤", f"{result['max_drawdown']:.2%}"),
        ("年化波动率", f"{result['volatility']:.2%}"),
        ("夏普比率", f"{result['sharpe']:.2f}"),
        ("成交笔数", str(len(result["trades"]))),
    ]
    metrics_html = "".join(f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>" for key, value in metrics)
    trade_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in (trade["date"].strftime("%Y-%m-%d"), trade["side"], f"{trade['price']:.3f}", trade["quantity"], f"{trade['fee']:.2f}")) + "</tr>"
        for trade in result["trades"]
    ) or '<tr><td colspan="5">区间内没有触发交易</td></tr>'
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>回测报告</title><style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;color:#1f2937;margin:24px;background:#f8fafc}}main{{max-width:1100px;margin:auto}}h1{{margin-bottom:4px}}.sub{{color:#64748b}}section{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:18px;margin:18px 0}}img{{width:100%;height:auto}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #e2e8f0;padding:9px;text-align:left}}th{{width:40%;color:#475569}}thead th{{background:#f1f5f9}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}</style></head><body><main><h1>可视化回测报告</h1><div class="sub">标的：{escape(code)}　基准：{escape(benchmark_code)}　策略：{escape(strategy_name)}　区间：{series.index[0]:%Y-%m-%d} 至 {series.index[-1]:%Y-%m-%d}</div><section><img alt="累计收益曲线" src="data:image/png;base64,{chart}"></section><div class="grid"><section><h2>绩效指标</h2><table>{metrics_html}</table></section><section><h2>交易记录</h2><table><thead><tr><th>日期</th><th>方向</th><th>成交价</th><th>数量</th><th>费用</th></tr></thead><tbody>{trade_rows}</tbody></table></section></div></main></body></html>"""
    output_file.write_text(html, encoding="utf-8")
    return output_file
