"""Volume-backed disclosure queries. No full-quarter Python objects survive a call."""

import math
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from employers import _normalise_employer

SCHEMA_VERSION = 1
MAX_RESULTS = 1000
AGENCIES = (
    "staffing|consulting|agency|infosys|tcs|wipro|cognizant|hcl|tech mahindra|accenture"
)
JOB_COLUMNS = ("JOB_TITLE", "SOC_TITLE", "JOB_TITLE_CLEAN")
WAGE_COLUMNS = ("WAGE_RATE_OF_PAY_FROM", "PREVAILING_WAGE", "WAGE_RATE_OF_PAY")


def quote(column: str) -> str:
    return '"' + column.replace('"', '""') + '"'


def configure(connection: sqlite3.Connection) -> None:
    # Per-connection page cache: 8 MiB. Sorting spills to disk; no mapped DB.
    connection.execute("PRAGMA cache_size=-8192")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA mmap_size=0")
    connection.execute("PRAGMA threads=1")


def numeric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


class DisclosureStore:
    """Only the path and column names are retained between operations."""

    def __init__(self, path: str):
        self.path = path
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ValueError("Unsupported or incomplete disclosure cache")
            self.columns = [
                r[1]
                for r in db.execute("PRAGMA table_info(disclosures)")
                if not r[1].startswith("_h1b_")
            ]
            self.row_count = db.execute("SELECT row_count FROM metadata").fetchone()[0]
        self.employer = next(
            (
                c
                for c in ("EMPLOYER_NAME", "EMPLOYER_BUSINESS_DBA")
                if c in self.columns
            ),
            None,
        )
        if self.employer is None:
            raise ValueError("Loaded H-1B data has no employer column")
        self.job = next((c for c in JOB_COLUMNS if c in self.columns), None)
        self.wage = next((c for c in WAGE_COLUMNS if c in self.columns), None)

    def __len__(self):
        return self.row_count

    @contextmanager
    def connect(self):
        db = sqlite3.connect(Path(self.path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            configure(db)
            # Compile once per distinct pattern, per operation (at most a few).
            patterns = {}

            def matches(pattern, value):
                if value is None:
                    return False
                if pattern not in patterns:
                    patterns[pattern] = re.compile(pattern, re.IGNORECASE)
                return patterns[pattern].search(str(value)) is not None

            db.create_function("regexp", 2, matches, deterministic=True)
            db.create_function(
                "casefold",
                1,
                lambda v: str(v).casefold() if v is not None else "",
                deterministic=True,
            )
            yield db
        finally:
            db.close()

    def _stats(self, db, where, args, period_label, source_url):
        employer = quote(self.employer)
        count = db.execute(
            f"SELECT count(*) FROM disclosures WHERE {where}", args
        ).fetchone()[0]
        if not count:
            return None
        name = db.execute(
            f"SELECT {employer} FROM disclosures WHERE {where} ORDER BY rowid LIMIT 1",
            args,
        ).fetchone()[0]
        stats = dict(
            company=name,
            total_applications=count,
            certified="N/A",
            denied="N/A",
            fiscal_periods=[period_label],
            data_version=period_label,
            source_url=source_url,
        )
        if "CASE_STATUS" in self.columns:
            counts = dict(
                db.execute(
                    f"SELECT casefold(CASE_STATUS), count(*) FROM disclosures WHERE {where} "
                    "GROUP BY casefold(CASE_STATUS)",
                    args,
                )
            )
            stats.update(
                certified=counts.get("certified", 0), denied=counts.get("denied", 0)
            )
            stats["certification_rate"] = round(stats["certified"] / count * 100, 2)
        if self.job:
            stats["top_job_titles"] = self._groups(db, self.job, where, args, 10)
        if self.wage:
            minimum, maximum, mean, n = db.execute(
                f"SELECT min(_h1b_wage), max(_h1b_wage), avg(_h1b_wage), count(_h1b_wage) "
                f"FROM disclosures WHERE {where}",
                args,
            ).fetchone()
            median = None
            if n:
                middle = db.execute(
                    f"SELECT _h1b_wage FROM disclosures WHERE ({where}) AND _h1b_wage IS NOT NULL "
                    "ORDER BY _h1b_wage LIMIT ? OFFSET ?",
                    (*args, 2 - n % 2, (n - 1) // 2),
                ).fetchall()
                median = sum(row[0] for row in middle) / len(middle)
            stats["wage_stats"] = dict(
                min=minimum, max=maximum, mean=mean, median=median
            )
        if "WORKSITE_STATE" in self.columns:
            stats["top_states"] = self._groups(db, "WORKSITE_STATE", where, args, 5)
        return stats

    @staticmethod
    def _groups(db, column, where, args, limit):
        col = quote(column)
        # Ties follow first occurrence, as pandas.value_counts does.
        return {
            str(value): count
            for value, count in db.execute(
                f"SELECT {col}, count(*) FROM disclosures WHERE ({where}) AND {col} IS NOT NULL "
                f"GROUP BY {col} ORDER BY count(*) DESC, min(rowid) LIMIT ?",
                (*args, limit),
            )
        }

    def company_stats(self, company_name, period_label, source_url):
        key = _normalise_employer(company_name)
        if not key:
            return None
        with self.connect() as db:
            return self._stats(
                db,
                "_h1b_employer_key IN (SELECT DISTINCT _h1b_employer_key FROM disclosures "
                "WHERE instr(_h1b_employer_key, ?) > 0)",
                (key,),
                period_label,
                source_url,
            )

    def search(
        self,
        job_role,
        city,
        state,
        min_wage,
        max_results,
        skip_agencies,
        period_label,
        source_url,
    ):
        if not 0 <= max_results <= MAX_RESULTS:
            return {"error": f"max_results must be between 0 and {MAX_RESULTS}"}
        clauses, args = [], []
        city_col = next(
            (c for c in ("WORKSITE_CITY", "EMPLOYER_CITY") if c in self.columns), None
        )
        state_col = next(
            (c for c in ("WORKSITE_STATE", "EMPLOYER_STATE") if c in self.columns), None
        )
        for column, pattern in ((self.job, job_role), (city_col, city)):
            if column and pattern is not None:
                try:
                    re.compile(pattern, re.IGNORECASE)
                except re.error:
                    return {"error": "Invalid search pattern"}
                clauses.append(f"regexp(?, {quote(column)})")
                args.append(pattern)
        if state and state_col:
            clauses.append(f"upper({quote(state_col)}) = ?")
            args.append(state.upper())
        if min_wage and self.wage:
            clauses.append("_h1b_wage >= ?")
            args.append(min_wage)
        if skip_agencies and "EMPLOYER_NAME" in self.columns:
            clauses.append("NOT regexp(?, EMPLOYER_NAME)")
            args.append(AGENCIES)
        if "CASE_STATUS" in self.columns:
            clauses.append("casefold(CASE_STATUS) = 'certified'")
        where = " AND ".join(clauses) or "1"
        results, company_stats = [], {}
        with self.connect() as db:
            total = db.execute(
                f"SELECT count(*) FROM disclosures WHERE {where}", args
            ).fetchone()[0]
            selected = [
                self.employer,
                self.job,
                city_col,
                state_col,
                self.wage,
                "EMPLOYER_POC_EMAIL",
                "CONTACT_EMAIL",
                "EMPLOYER_PHONE",
            ]
            columns = list(dict.fromkeys(c for c in selected if c in self.columns))
            rows = db.execute(
                f"SELECT {', '.join(map(quote, columns))} FROM disclosures "
                f"WHERE {where} ORDER BY rowid LIMIT ?",
                (*args, max_results),
            ).fetchall()
            for values in rows:
                row = dict(zip(columns, values))
                result = dict(
                    employer=row.get(self.employer, "Unknown"),
                    job_title=row.get(self.job, "Unknown"),
                    city=row.get(city_col, "Unknown"),
                    state=row.get(state_col, "Unknown"),
                )
                key = _normalise_employer(result["employer"])
                if key not in company_stats:
                    company_stats[key] = self._stats(
                        db, "_h1b_employer_key = ?", (key,), period_label, source_url
                    )
                if company_stats[key]:
                    result["company_stats"] = company_stats[key]
                if self.wage:
                    result["wage"] = row[self.wage]
                for field in ("EMPLOYER_POC_EMAIL", "CONTACT_EMAIL", "EMPLOYER_PHONE"):
                    if row.get(field) is not None:
                        result["contact"] = row[field]
                        break
                results.append(result)
        return dict(
            total_matches=total,
            returned=len(results),
            results=results,
            fiscal_periods=[period_label],
            data_version=period_label,
            source_url=source_url,
        )

    def top_sponsors(self, limit, exclude_agencies):
        if not 0 <= limit <= MAX_RESULTS:
            return {"error": f"limit must be between 0 and {MAX_RESULTS}"}
        col = quote(self.employer)
        where = f"NOT regexp(?, {col})" if exclude_agencies else "1"
        args = (AGENCIES,) if exclude_agencies else ()
        results = []
        with self.connect() as db:
            total = db.execute(
                f"SELECT count(DISTINCT {col}) FROM disclosures WHERE {where}", args
            ).fetchone()[0]
            certified = (
                "sum(casefold(CASE_STATUS) = 'certified')"
                if "CASE_STATUS" in self.columns
                else "count(*)"
            )
            top = db.execute(
                f"SELECT {col}, count(*), {certified}, avg(_h1b_wage) FROM disclosures "
                f"WHERE ({where}) AND {col} IS NOT NULL GROUP BY {col} "
                "ORDER BY count(*) DESC, min(rowid) LIMIT ?",
                (*args, limit),
            ).fetchall()
            for company, count, certified_count, mean in top:
                result = dict(
                    company=company,
                    total_applications=count,
                    certified=certified_count,
                )
                if self.wage:
                    result["avg_wage"] = mean
                if "WORKSITE_STATE" in self.columns:
                    state = db.execute(
                        f"SELECT WORKSITE_STATE FROM disclosures WHERE {col} = ? "
                        "AND WORKSITE_STATE IS NOT NULL GROUP BY WORKSITE_STATE "
                        "ORDER BY count(*) DESC, WORKSITE_STATE LIMIT 1",
                        (company,),
                    ).fetchone()
                    result["primary_state"] = state[0] if state else "N/A"
                results.append(result)
        return dict(top_sponsors=results, total_companies=total)
