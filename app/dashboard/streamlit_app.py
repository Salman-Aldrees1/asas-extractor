from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT_DIR = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT_DIR / "output"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_financial_data() -> tuple[pd.DataFrame, pd.DataFrame, float]:
    excel_snapshot = _read_json(OUTPUT_DIR / "excel_financials_snapshot.json")
    validation = _read_json(OUTPUT_DIR / "validation_report.json")

    income = excel_snapshot.get("income_statement", {})
    cash_flow = excel_snapshot.get("cash_flow", {})

    metric_map = {
        "Revenue": "Revenue",
        "Gross Profit": "Gross Profit",
        "Operating profit": "Operating Profit",
        "Net profit for the period": "Net Profit",
    }

    trend_rows: list[dict] = []
    for source_metric, display_metric in metric_map.items():
        for period, value in income.get(source_metric, {}).items():
            if "FY-" not in period:
                continue
            year = period.replace("FY-", "").split(" ")[0]
            trend_rows.append(
                {
                    "year": int(year),
                    "metric": display_metric,
                    "value": float(value),
                }
            )

    cash_rows: list[dict] = []
    for source_metric, yearly_values in cash_flow.items():
        if source_metric == "Cash and cash equivalents at the end of the year":
            metric = "Cash End Of Year"
        elif source_metric == "Net cash flows generated from operating activities":
            metric = "Operating Cash Flow"
        elif source_metric == "Net cash flows used in investing activities":
            metric = "Investing Cash Flow"
        elif source_metric == "Net cash flows used in financing activities":
            metric = "Financing Cash Flow"
        else:
            continue

        for period, value in yearly_values.items():
            if "FY-" not in period:
                continue
            year = period.replace("FY-", "").split(" ")[0]
            cash_rows.append(
                {
                    "year": int(year),
                    "metric": metric,
                    "value": float(value),
                }
            )

    trend_df = pd.DataFrame(trend_rows)
    cash_df = pd.DataFrame(cash_rows)

    if trend_df.empty:
        trend_df = pd.DataFrame(
            [
                {"year": 2022, "metric": "Revenue", "value": 588_382_740},
                {"year": 2023, "metric": "Revenue", "value": 767_023_097},
                {"year": 2024, "metric": "Revenue", "value": 926_002_004},
                {"year": 2022, "metric": "Net Profit", "value": 125_346_716},
                {"year": 2023, "metric": "Net Profit", "value": 148_677_253},
                {"year": 2024, "metric": "Net Profit", "value": 156_958_529},
            ]
        )

    if cash_df.empty:
        cash_df = pd.DataFrame(
            [
                {"year": 2022, "metric": "Operating Cash Flow", "value": 236_121_805},
                {"year": 2023, "metric": "Operating Cash Flow", "value": 169_277_441},
                {"year": 2024, "metric": "Operating Cash Flow", "value": 220_629_945},
            ]
        )

    match_rate = float(validation.get("summary", {}).get("match_rate", 0.0))
    return trend_df, cash_df, match_rate


def _format_mn(value: float) -> str:
    return f"{value / 1_000_000:,.1f}M"


def main() -> None:
    st.set_page_config(page_title="Asas Financials MVP", layout="wide")

    st.title("Asas Financials Platform — MVP Dashboard")
    st.caption("Demo mode · Annual data · Currency: SAR")

    trend_df, cash_df, match_rate = _load_financial_data()

    available_metrics = sorted(trend_df["metric"].unique().tolist())
    available_years = sorted(trend_df["year"].unique().tolist())

    with st.sidebar:
        st.header("Filters")
        company = st.selectbox("Company", ["Sample Company"], index=0)
        selected_metrics = st.multiselect(
            "Metrics",
            available_metrics,
            default=[m for m in ["Revenue", "Net Profit", "Operating Profit"] if m in available_metrics]
            or available_metrics,
        )
        selected_years = st.slider(
            "Years",
            min_value=min(available_years),
            max_value=max(available_years),
            value=(min(available_years), max(available_years)),
        )
        st.caption(f"Selected: {company}")

    filtered = trend_df[
        (trend_df["metric"].isin(selected_metrics))
        & (trend_df["year"] >= selected_years[0])
        & (trend_df["year"] <= selected_years[1])
    ]

    latest_year = int(trend_df["year"].max())
    latest_data = trend_df[trend_df["year"] == latest_year]

    revenue_latest = latest_data.loc[latest_data["metric"] == "Revenue", "value"]
    net_latest = latest_data.loc[latest_data["metric"] == "Net Profit", "value"]
    margin = (net_latest.iloc[0] / revenue_latest.iloc[0] * 100) if not revenue_latest.empty and not net_latest.empty else 0.0

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Revenue (latest)", _format_mn(revenue_latest.iloc[0]) if not revenue_latest.empty else "n/a")
    col2.metric("Net Profit (latest)", _format_mn(net_latest.iloc[0]) if not net_latest.empty else "n/a")
    col3.metric("Net Margin", f"{margin:.1f}%")
    col4.metric("Data Match Rate", f"{match_rate:.2f}%")

    tab_summary, tab_financials, tab_quality = st.tabs(["Summary", "Financial Statements", "Data Quality"])

    with tab_summary:
        st.subheader("Financial Trend")
        fig = px.line(
            filtered.sort_values("year"),
            x="year",
            y="value",
            color="metric",
            markers=True,
            labels={"value": "SAR", "year": "Year"},
        )
        fig.update_layout(legend_title_text="Metric", margin=dict(l=20, r=20, t=20, b=20))
        st.plotly_chart(fig, use_container_width=True)

    with tab_financials:
        st.subheader("Cash Flow Overview")
        cash_filtered = cash_df[
            (cash_df["year"] >= selected_years[0])
            & (cash_df["year"] <= selected_years[1])
        ].sort_values("year")

        cash_fig = px.bar(
            cash_filtered,
            x="year",
            y="value",
            color="metric",
            barmode="group",
            labels={"value": "SAR", "year": "Year"},
        )
        cash_fig.update_layout(margin=dict(l=20, r=20, t=20, b=20))
        st.plotly_chart(cash_fig, use_container_width=True)

        st.subheader("Raw Financial Table")
        table_df = filtered.copy()
        table_df["value"] = table_df["value"].map(lambda x: f"{x:,.0f}")
        st.dataframe(table_df.rename(columns={"year": "Year", "metric": "Metric", "value": "Value (SAR)"}), use_container_width=True)

    with tab_quality:
        st.subheader("Extraction Validation Snapshot")
        quality_text = "Good for demo" if match_rate >= 90 else "Needs review"
        st.info(f"Current validation match rate: {match_rate:.2f}% · Status: {quality_text}")
        st.markdown(
            "- MVP rule: continue delivery if extraction quality is stable and gaps are explainable.\n"
            "- Current known gaps are concentrated in 2022 cash-flow lines.\n"
            "- Next iteration: expose per-metric confidence flags in UI."
        )


if __name__ == "__main__":
    main()
