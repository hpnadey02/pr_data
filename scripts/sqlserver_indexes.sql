/* ============================================================================
   Indexing for dbo.May_2 at production scale (~27M rows x 124 columns, 4 years)

   Run this ONCE, with your DBA, before pointing the chatbot at the full table.
   Without it every question is an 11 GB table scan and DB_QUERY_TIMEOUT expires
   before SQL Server returns anything.

   Review each statement before running. Index creation is a write operation on a
   large table: it takes time, needs space, and should be scheduled off-peak.
   ============================================================================ */


/* ----------------------------------------------------------------------------
   0. Measure first, so you can prove the change worked.
   ---------------------------------------------------------------------------- */
SET STATISTICS TIME ON;
SET STATISTICS IO ON;

SELECT TOP 10 [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total_gross_premium
FROM dbo.May_2
GROUP BY [BRANCH_NAME]
ORDER BY total_gross_premium DESC;

-- Note the elapsed time and logical reads. Re-run this at the end to compare.


/* ----------------------------------------------------------------------------
   1. CLUSTERED COLUMNSTORE INDEX  <-- the single biggest win

   This table is a pure analytics workload: aggregate over millions of rows,
   touching only a handful of the 124 columns. That is exactly what columnstore
   is built for.

     * ~10x compression (roughly 11 GB -> ~1 GB)
     * only the columns named in the query are read, not whole rows
     * batch-mode execution for GROUP BY / SUM

   Expect aggregate queries to go from tens of seconds to low single digits.

   TRADE-OFF: single-row lookups get slower under columnstore, which is why the
   nonclustered indexes in step 2 are added alongside it. If this table is
   append-only (a monthly load), columnstore is the right default. If it is
   updated row-by-row throughout the day, discuss with your DBA first.
   ---------------------------------------------------------------------------- */

-- DROP any existing clustered index first if one exists; a table can have only one.
CREATE CLUSTERED COLUMNSTORE INDEX CCI_May_2
    ON dbo.May_2
    WITH (DROP_EXISTING = OFF, MAXDOP = 4);
GO


/* ----------------------------------------------------------------------------
   2. Nonclustered indexes for the single-record LOOKUP route

   The chatbot routes "POLICY_NO-1029156133, give USGI_SUM_INSURED" to a lookup.
   Under columnstore alone that is still a full scan; these make it a seek.
   ---------------------------------------------------------------------------- */

CREATE NONCLUSTERED INDEX IX_May_2_POLICY_NO
    ON dbo.May_2 ([POLICY_NO]);
GO

CREATE NONCLUSTERED INDEX IX_May_2_POLICY_NO_CHAR
    ON dbo.May_2 ([POLICY_NO_CHAR]);
GO

CREATE NONCLUSTERED INDEX IX_May_2_REFERENCE_NUMBER
    ON dbo.May_2 ([REFERENCE_NUMBER]);
GO

CREATE NONCLUSTERED INDEX IX_May_2_USGIpos_Policy_Number
    ON dbo.May_2 ([USGIpos_Policy_Number]);
GO


/* ----------------------------------------------------------------------------
   3. Date index

   Every week/month/yearly-trend question filters on POLICY_ISSUE_DATE, and
   date_windows.py runs MAX([POLICY_ISSUE_DATE]) to find the latest month in the
   data. Both become instant with this.
   ---------------------------------------------------------------------------- */

CREATE NONCLUSTERED INDEX IX_May_2_POLICY_ISSUE_DATE
    ON dbo.May_2 ([POLICY_ISSUE_DATE]);
GO

-- Add these only if users actually ask date questions against them.
-- CREATE NONCLUSTERED INDEX IX_May_2_START_DATE   ON dbo.May_2 ([START_DATE]);
-- CREATE NONCLUSTERED INDEX IX_May_2_EXPIRY_DATE  ON dbo.May_2 ([EXPIRY_DATE]);
-- CREATE NONCLUSTERED INDEX IX_May_2_VOUCHER_DATE ON dbo.May_2 ([VOUCHER_DATE]);
GO


/* ----------------------------------------------------------------------------
   4. Statistics

   The optimiser needs current statistics to pick a good plan. After a bulk load,
   refresh them.
   ---------------------------------------------------------------------------- */

UPDATE STATISTICS dbo.May_2 WITH FULLSCAN;
GO


/* ----------------------------------------------------------------------------
   5. Verify

   Re-run the query from step 0 and compare elapsed time and logical reads.
   Then set DB_QUERY_TIMEOUT in .env to roughly 4x the slowest measured query -
   enough headroom for a cold cache, without leaving a runaway query holding a
   worker for minutes.
   ---------------------------------------------------------------------------- */

SELECT TOP 10 [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total_gross_premium
FROM dbo.May_2
GROUP BY [BRANCH_NAME]
ORDER BY total_gross_premium DESC;

SET STATISTICS TIME OFF;
SET STATISTICS IO OFF;


/* ----------------------------------------------------------------------------
   6. What the indexes look like afterwards
   ---------------------------------------------------------------------------- */

SELECT i.name          AS index_name,
       i.type_desc     AS index_type,
       SUM(p.rows)     AS row_count,
       CAST(SUM(a.total_pages) * 8.0 / 1024 / 1024 AS DECIMAL(10, 2)) AS size_gb
FROM sys.indexes i
JOIN sys.partitions p      ON i.object_id = p.object_id AND i.index_id = p.index_id
JOIN sys.allocation_units a ON p.partition_id = a.container_id
WHERE i.object_id = OBJECT_ID('dbo.May_2')
GROUP BY i.name, i.type_desc
ORDER BY size_gb DESC;
