from __future__ import annotations

import os
from typing import Any

import sahmk
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app.storage.models import Company, FinancialMetric

load_dotenv()


class SahmkEnricher:
    """SAHMK API enricher for ingestion pipeline - adds market data and company info."""
    
    def __init__(self):
        api_key = os.getenv("SAHMK_API_KEY")
        if not api_key:
            raise ValueError("SAHMK_API_KEY not found in environment")
        self.client = sahmk.SahmkClient(api_key=api_key)
    
    def enrich_company(self, db: Session, company: Company) -> dict[str, Any]:
        """Enrich a company with SAHMK market data and return enrichment summary."""
        results = {
            "company_id": company.id,
            "ticker": company.ticker,
            "enrichments": {},
            "errors": [],
            "metrics_added": 0
        }
        
        try:
            # Try to get company info and quote
            symbol = self._map_ticker_to_symbol(company.ticker)
            if not symbol:
                results["errors"].append("Could not map ticker to SAHMK symbol")
                return results
            
            # Get company info
            try:
                sahm_company = self.client.company(symbol)
                results["enrichments"]["company_info"] = {
                    "name": sahm_company.name,
                    "sector": sahm_company.sector,
                    "industry": getattr(sahm_company, "industry", None),
                    "market_cap": getattr(sahm_company, "market_cap", None)
                }
                
                # Update company record if we have better data
                if sahm_company.name and sahm_company.name != company.name:
                    company.name = sahm_company.name
                if sahm_company.sector and sahm_company.sector != company.sector:
                    company.sector = sahm_company.sector
                    
            except Exception as e:
                results["errors"].append(f"Failed to get company info: {e}")
            
            # Get real-time quote
            try:
                quote = self.client.quote(symbol)
                quote_data = {
                    "price": quote.price,
                    "change": quote.change,
                    "change_percent": getattr(quote, "change_percent", None),
                    "volume": getattr(quote, "volume", None),
                    "market_cap": getattr(quote, "market_cap", None)
                }
                results["enrichments"]["quote"] = quote_data
                
                # Store as financial metrics with current year
                current_year = 2024  # Could be dynamic
                self._upsert_metric(db, company.id, "market_data", "Current Price", current_year, quote.price, "sahmk")
                self._upsert_metric(db, company.id, "market_data", "Daily Change", current_year, quote.change, "sahmk")
                if quote_data.get("volume"):
                    self._upsert_metric(db, company.id, "market_data", "Daily Volume", current_year, quote.volume, "sahmk")
                if quote_data.get("market_cap"):
                    self._upsert_metric(db, company.id, "market_data", "Market Cap", current_year, quote.market_cap, "sahmk")
                    
            except Exception as e:
                results["errors"].append(f"Failed to get quote: {e}")
            
            # Try to get financials if available (requires paid plan)
            try:
                financials = self.client.financials(symbol)
                if hasattr(financials, 'income_statements') and financials.income_statements:
                    latest_income = financials.income_statements[0]
                    fin_data = {
                        "period": latest_income.period,
                        "revenue": getattr(latest_income, "revenue", None),
                        "net_income": getattr(latest_income, "net_income", None),
                        "eps": getattr(latest_income, "eps", None)
                    }
                    results["enrichments"]["financials"] = fin_data
                    
                    # Store financial metrics
                    period_year = self._extract_year_from_period(latest_income.period)
                    if fin_data["revenue"]:
                        self._upsert_metric(db, company.id, "income_statement", "Revenue", period_year, fin_data["revenue"], "sahmk")
                    if fin_data["net_income"]:
                        self._upsert_metric(db, company.id, "income_statement", "Net Income", period_year, fin_data["net_income"], "sahmk")
                    
            except Exception as e:
                # Expected for free tier - not an error
                results["enrichments"]["financials"] = f"Not available (free tier): {e}"
            
        except Exception as e:
            results["errors"].append(f"Unexpected error: {e}")
        
        return results
    
    def get_market_summary(self) -> dict[str, Any]:
        """Get overall market summary for dashboard."""
        try:
            market = self.client.market_summary()
            return {
                "index_value": market.index_value,
                "index_change": market.index_change,
                "index_change_percent": market.index_change_percent,
                "total_volume": market.total_volume,
                "advancing": market.advancing,
                "declining": market.declining,
                "unchanged": market.unchanged,
                "market_mood": market.market_mood,
                "timestamp": market.timestamp
            }
        except Exception as e:
            return {"error": str(e)}
    
    def get_top_movers(self, limit: int = 5) -> dict[str, Any]:
        """Get top gainers and losers for market context."""
        try:
            gainers = self.client.gainers(limit=limit)
            losers = self.client.losers(limit=limit)
            
            return {
                "gainers": [
                    {"symbol": s.symbol, "change": s.change, "price": getattr(s, "price", None)}
                    for s in gainers.stocks
                ],
                "losers": [
                    {"symbol": s.symbol, "change": s.change, "price": getattr(s, "price", None)}
                    for s in losers.stocks
                ]
            }
        except Exception as e:
            return {"error": str(e)}
    
    def _map_ticker_to_symbol(self, ticker: str) -> str | None:
        """Map our internal ticker to SAHMK symbol."""
        # Basic mapping - could be expanded or made configurable
        ticker_mappings = {
            "ASAS": "2222",  # Aramco as example
            "SRG": "1120",   # Saudi Retail as example
            # Add more mappings as needed
        }
        return ticker_mappings.get(ticker.upper(), ticker.upper())
    
    def _extract_year_from_period(self, period: str) -> int:
        """Extract year from period string like '2023' or '2023-12-31'."""
        if "-" in period:
            return int(period.split("-")[0])
        return int(period) if period.isdigit() else 2024
    
    def _upsert_metric(self, db: Session, company_id: int, statement: str, metric: str, year: int, value: float, source: str) -> None:
        """Upsert a financial metric."""
        from sqlalchemy import and_, delete
        
        # Delete existing metric for this company/statement/metric/year/source
        db.execute(
            delete(FinancialMetric).where(
                and_(
                    FinancialMetric.company_id == company_id,
                    FinancialMetric.statement == statement,
                    FinancialMetric.metric == metric,
                    FinancialMetric.year == year,
                    FinancialMetric.source == source,
                )
            )
        )
        
        # Add new metric
        db.add(
            FinancialMetric(
                company_id=company_id,
                statement=statement,
                metric=metric,
                year=year,
                value=value,
                currency="SAR",
                source=source,
            )
        )
