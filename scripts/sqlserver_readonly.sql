/* ============================================================================
   Read-only access for the chatbot's SQL Server login on dbo.May_2

   The chatbot only ever READS. This script makes the database enforce that, so
   no user and no developer can change the table's data through the chatbot,
   whatever SQL reaches the server:

     Part 1  Least privilege - a role that may SELECT dbo.May_2 and is DENIED
             every write on it.
     Part 2  Row-Level Security - a BLOCK-predicate security policy that refuses
             INSERT / UPDATE / DELETE on dbo.May_2 when the chatbot's login is
             the one connected. No FILTER predicate: reads are not touched.
     Part 3  Verification - read-only queries that show the result.

   The app has its own guards too (backend/core/read_only.py): generated SQL
   containing INSERT, DELETE, SELECT ... INTO and similar is refused before it
   is ever sent. This script is the layer that still holds if those are bypassed.

   HOW TO RUN (plain SSMS - no SQLCMD mode needed)
     1. Connect as a sysadmin, or as db_owner of the database that holds the table.
     2. Select that database in the SSMS toolbar dropdown.
     3. Edit the "EDIT THESE" block below. @login_name is DB_USERNAME from .env.
     4. First run with @apply_changes = 0: it changes nothing and only reports.
     5. Set @apply_changes = 1 and run the WHOLE script (F5, nothing highlighted).
   The script is one batch (there is no GO), because variables do not survive a
   GO. Every statement that must be alone in a batch runs as dynamic SQL.
   It is safe to run again; each run converges on the same end state.

   BEFORE YOU RUN - READ THIS
     * The login must be used ONLY by the chatbot. If the job that loads
       dbo.May_2 connects with the same login, this script blocks that job too.
       Give the chatbot its own login first.
     * sysadmin skips every permission check: the DENYs in Part 1 do nothing for
       a sysadmin login, and a sysadmin (or db_owner) can simply switch the
       Part 2 policy off. The script PRINTs a warning in those cases and never
       changes role memberships itself - that is the DBA's decision.
     * Part 2 needs SQL Server 2016 (13.x) or later, or Azure SQL. On older
       versions Part 2 is skipped and Part 1 still applies.

   NOT YET VERIFIED against a live SQL Server by its author. Run it with
   @apply_changes = 0 first and read the Messages tab.
   ============================================================================ */

SET NOCOUNT ON;
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;

-- USE [YourDatabase];    -- alternative to the SSMS dropdown

/* ------------------------------- EDIT THESE ------------------------------- */
DECLARE @login_name      sysname = N'usgi_chatbot';           -- DB_USERNAME in .env
DECLARE @user_name       sysname = N'usgi_chatbot';           -- database user of that login
DECLARE @table_schema    sysname = N'dbo';                    -- DB_TABLE = dbo.May_2
DECLARE @table_name      sysname = N'May_2';
DECLARE @role_name       sysname = N'usgi_chatbot_readonly';
DECLARE @security_schema sysname = N'usgi_security';
DECLARE @apply_changes   bit     = 0;                         -- 0 = report only, 1 = apply
/* -------------------------------------------------------------------------- */

DECLARE @table_q    nvarchar(300) = QUOTENAME(@table_schema) + N'.' + QUOTENAME(@table_name);
DECLARE @role_q     nvarchar(300) = QUOTENAME(@role_name);
DECLARE @user_q     nvarchar(300) = QUOTENAME(@user_name);
DECLARE @security_q nvarchar(300) = QUOTENAME(@security_schema);
DECLARE @function_q nvarchar(300) = QUOTENAME(@security_schema) + N'.' + QUOTENAME(N'fn_chatbot_write_block');
DECLARE @policy_q   nvarchar(300) = QUOTENAME(@security_schema) + N'.' + QUOTENAME(N'chatbot_readonly_policy');
DECLARE @login_sid  varbinary(85) = SUSER_SID(@login_name);
DECLARE @user_sid   varbinary(85);
DECLARE @sql        nvarchar(max);
DECLARE @problems   int = 0;
DECLARE @other_block_predicates int = 0;
DECLARE @impersonated nvarchar(10) = NULL;
DECLARE @major_version int =
    CAST(PARSENAME(CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)), 4) AS int);
DECLARE @engine_edition int = CAST(SERVERPROPERTY('EngineEdition') AS int);
-- EngineEdition 5 = Azure SQL Database, 8 = Azure SQL Managed Instance.
DECLARE @rls_supported bit =
    CASE WHEN @major_version >= 13 OR @engine_edition IN (5, 8) THEN 1 ELSE 0 END;

SELECT @user_sid = sid FROM sys.database_principals WHERE name = @user_name;

PRINT N'Database : ' + QUOTENAME(DB_NAME());
PRINT N'Table    : ' + @table_q;
PRINT N'Login    : ' + QUOTENAME(@login_name) + N'   database user: ' + @user_q;
PRINT N'Mode     : ' + CASE WHEN @apply_changes = 1 THEN N'APPLY CHANGES' ELSE N'REPORT ONLY (set @apply_changes = 1 to apply)' END;
PRINT N'';


/* ============================================================================
   0. Stop early on a wrong database, table, login or user
   ============================================================================ */

IF OBJECT_ID(@table_q, N'U') IS NULL
BEGIN
    PRINT N'STOPPED: table ' + @table_q + N' does not exist in ' + QUOTENAME(DB_NAME())
        + N'. Pick the right database in the SSMS dropdown, or fix @table_schema / @table_name.';
    RETURN;
END;

IF @login_sid IS NULL
BEGIN
    PRINT N'STOPPED: there is no server login named ' + QUOTENAME(@login_name)
        + N'. Set @login_name to DB_USERNAME from the chatbot''s .env.';
    RETURN;
END;

IF @user_sid IS NULL
BEGIN
    PRINT N'STOPPED: there is no database user named ' + @user_q + N' in ' + QUOTENAME(DB_NAME())
        + N'. Find the user mapped to the login with:  SELECT name FROM sys.database_principals WHERE sid = SUSER_SID(N'''
        + REPLACE(@login_name, N'''', N'''''') + N''');';
    RETURN;
END;

IF @user_sid <> @login_sid
BEGIN
    PRINT N'STOPPED: database user ' + @user_q + N' is not mapped to login ' + QUOTENAME(@login_name)
        + N'. Part 1 applies to the user and Part 2 to the login, so they must be the same account.';
    RETURN;
END;


/* ============================================================================
   Memberships that would defeat this script. Reported, never changed.
   ============================================================================ */

IF IS_SRVROLEMEMBER(N'sysadmin', @login_name) = 1
BEGIN
    SET @problems += 1;
    PRINT N'WARNING: login ' + QUOTENAME(@login_name) + N' is a member of sysadmin. sysadmin skips every'
        + N' permission check, so the Part 1 DENYs do nothing for it, and it can switch the Part 2 policy'
        + N' off. Give the chatbot a login that is not sysadmin.';
END;

IF @user_name = N'dbo'
   OR EXISTS (SELECT 1 FROM sys.databases WHERE database_id = DB_ID() AND owner_sid = @login_sid)
BEGIN
    SET @problems += 1;
    PRINT N'WARNING: ' + QUOTENAME(@login_name) + N' owns this database, so it connects as dbo and'
        + N' permission checks are skipped. Change the owner, e.g.  ALTER AUTHORIZATION ON DATABASE::'
        + QUOTENAME(DB_NAME()) + N' TO [sa];';
END;

IF IS_ROLEMEMBER(N'db_owner', @user_name) = 1
BEGIN
    SET @problems += 1;
    PRINT N'WARNING: ' + @user_q + N' is in db_owner, which can undo every line of this script.'
        + N' To remove:  ALTER ROLE [db_owner] DROP MEMBER ' + @user_q + N';';
END;

IF IS_ROLEMEMBER(N'db_datawriter', @user_name) = 1
BEGIN
    SET @problems += 1;
    PRINT N'WARNING: ' + @user_q + N' is in db_datawriter (INSERT/UPDATE/DELETE on every table).'
        + N' The DENYs below override it for ' + @table_q + N' but not for other tables.'
        + N' To remove:  ALTER ROLE [db_datawriter] DROP MEMBER ' + @user_q + N';';
END;

IF IS_ROLEMEMBER(N'db_ddladmin', @user_name) = 1
BEGIN
    SET @problems += 1;
    PRINT N'WARNING: ' + @user_q + N' is in db_ddladmin (can create, alter and drop objects).'
        + N' To remove:  ALTER ROLE [db_ddladmin] DROP MEMBER ' + @user_q + N';';
END;

IF EXISTS (SELECT 1 FROM sys.schemas
           WHERE name = @table_schema AND principal_id = DATABASE_PRINCIPAL_ID(@user_name))
   OR EXISTS (SELECT 1 FROM sys.objects
              WHERE object_id = OBJECT_ID(@table_q) AND principal_id = DATABASE_PRINCIPAL_ID(@user_name))
BEGIN
    SET @problems += 1;
    PRINT N'WARNING: ' + @user_q + N' owns ' + @table_q + N' or its schema. An owner is not subject to'
        + N' DENY. Transfer ownership to dbo.';
END;

IF IS_ROLEMEMBER(N'db_datareader', @user_name) = 1
    PRINT N'NOTE: ' + @user_q + N' is in db_datareader (SELECT on every table). Not a write risk;'
        + N' removing it is optional hardening - the role below grants SELECT on ' + @table_q + N' only.';


/* ============================================================================
   PART 1 - Least privilege

   Everything is granted to / denied to a ROLE, never to the user directly, so
   the rollback at the end is simply "drop the role".

   Why DENY and not just "don't GRANT": a DENY beats every GRANT the user gets
   from anywhere else (db_datawriter, a GRANT to public, a forgotten direct
   GRANT). It does not bind sysadmin, dbo or the object's owner - see the
   warnings above.

   Why NOT "DENY CONTROL": CONTROL implies SELECT, and denying CONTROL on the
   table would deny SELECT with it - the chatbot could no longer read.

   TRUNCATE TABLE needs ALTER on the table; DROP TABLE needs CONTROL on it or
   ALTER on its schema. Denying ALTER on both covers them. REFERENCES is denied
   because it lets a login bind new objects (foreign keys, schema-bound views)
   to the table, which then blocks changes to the table itself.
   ============================================================================ */

IF @apply_changes = 1
BEGIN
    BEGIN TRY
        BEGIN TRANSACTION;

        IF DATABASE_PRINCIPAL_ID(@role_name) IS NULL
        BEGIN
            SET @sql = N'CREATE ROLE ' + @role_q + N' AUTHORIZATION [dbo];';
            EXEC sys.sp_executesql @sql;
        END;

        SET @sql = N'GRANT SELECT ON OBJECT::' + @table_q + N' TO ' + @role_q + N';';
        EXEC sys.sp_executesql @sql;

        SET @sql = N'DENY INSERT, UPDATE, DELETE, ALTER, TAKE OWNERSHIP, REFERENCES ON OBJECT::'
            + @table_q + N' TO ' + @role_q + N';';
        EXEC sys.sp_executesql @sql;

        SET @sql = N'DENY ALTER ON SCHEMA::' + QUOTENAME(@table_schema) + N' TO ' + @role_q + N';';
        EXEC sys.sp_executesql @sql;

        SET @sql = N'DENY CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE FUNCTION TO '
            + @role_q + N';';
        EXEC sys.sp_executesql @sql;

        IF NOT EXISTS (SELECT 1 FROM sys.database_role_members
                       WHERE role_principal_id = DATABASE_PRINCIPAL_ID(@role_name)
                         AND member_principal_id = DATABASE_PRINCIPAL_ID(@user_name))
        BEGIN
            SET @sql = N'ALTER ROLE ' + @role_q + N' ADD MEMBER ' + @user_q + N';';
            EXEC sys.sp_executesql @sql;
        END;

        COMMIT TRANSACTION;
        PRINT N'Part 1 applied: ' + @role_q + N' may SELECT ' + @table_q
            + N' and is denied INSERT, UPDATE, DELETE, ALTER; ' + @user_q + N' is a member.';
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
        PRINT N'Part 1 FAILED and was rolled back: ' + ERROR_MESSAGE();
        THROW;
    END CATCH;
END
ELSE
    PRINT N'Part 1 skipped (report only).';


/* ============================================================================
   PART 2 - Row-Level Security: block every write from the chatbot's login

   A security policy with BLOCK predicates only - AFTER INSERT, AFTER UPDATE,
   BEFORE UPDATE, BEFORE DELETE - on the table. The predicate function returns
   a row ("allowed") for every login EXCEPT the chatbot's, so:
     * the chatbot's INSERT / UPDATE / DELETE / MERGE on the table fails with
       error 33504, whatever permissions it holds;
     * every other login - the job that loads the table, the DBA - is untouched;
     * there is no FILTER predicate, so SELECT is not affected and no read pays
       anything for it. Block predicates are only evaluated on writes.

   Why ORIGINAL_LOGIN() and not SUSER_SNAME(): ORIGINAL_LOGIN() is the login
   that opened the connection and does not change under EXECUTE AS. With
   SUSER_SNAME(), the chatbot could step around the block by impersonating
   another principal, or through any module declared WITH EXECUTE AS OWNER.
   The flip side: you cannot see this block by testing with EXECUTE AS from
   your own session - ORIGINAL_LOGIN() is still you. That is why Part 3 does not
   try, and why scripts/check_readonly_access.py (which connects AS the chatbot)
   is the real test.

   The comparison is case-insensitive on purpose: SQL Server login names are
   matched that way, and a case-sensitive database collation would otherwise
   let 'USGI_Chatbot' slip past a predicate written for 'usgi_chatbot'.

   The policy passes a constant (1), not a column: the decision depends only on
   who is connected, and binding no column means the loading job can still
   alter any column of the table.

   What RLS does NOT stop: TRUNCATE TABLE and DROP TABLE are not row
   operations - Part 1's DENY ALTER covers those. A security policy also cannot
   be added while an indexed view references the table; Part 2 then fails and
   rolls back, and Part 1 still stands.

   The loading job is allowed through, but every row it writes now runs this
   one-line login comparison. That should be negligible; still, time the next
   load once, and if the job loads by ALTER TABLE ... SWITCH, test it after
   Part 2 before relying on it.

   The function and the policy are dropped and recreated on every run, inside
   one transaction: the policy is schema-bound to the function, so the function
   cannot change while the policy exists, and the transaction means there is no
   moment without a policy.
   ============================================================================ */

IF @rls_supported = 1
BEGIN
    SET @sql = N'SELECT @n = COUNT(*)
FROM sys.security_predicates AS pr
JOIN sys.security_policies AS p ON p.object_id = pr.object_id
WHERE pr.target_object_id = OBJECT_ID(@t)
  AND pr.predicate_type = 1
  AND p.is_enabled = 1
  AND p.object_id <> ISNULL(OBJECT_ID(@policy), 0);';
    EXEC sys.sp_executesql @sql,
        N'@t nvarchar(300), @policy nvarchar(300), @n int OUTPUT',
        @t = @table_q, @policy = @policy_q, @n = @other_block_predicates OUTPUT;

    IF @other_block_predicates > 0
        PRINT N'NOTE: another enabled security policy already has BLOCK predicates on ' + @table_q
            + N'. If Part 2 fails with a conflict, see the 3b result set for which policy it is.';
END;

IF @apply_changes = 1 AND @rls_supported = 1
BEGIN
    BEGIN TRY
        BEGIN TRANSACTION;

        IF SCHEMA_ID(@security_schema) IS NULL
        BEGIN
            SET @sql = N'CREATE SCHEMA ' + @security_q + N' AUTHORIZATION [dbo];';
            EXEC sys.sp_executesql @sql;
        END;

        IF OBJECT_ID(@policy_q, N'SP') IS NOT NULL
        BEGIN
            SET @sql = N'DROP SECURITY POLICY ' + @policy_q + N';';
            EXEC sys.sp_executesql @sql;
        END;

        IF OBJECT_ID(@function_q, N'IF') IS NOT NULL
        BEGIN
            SET @sql = N'DROP FUNCTION ' + @function_q + N';';
            EXEC sys.sp_executesql @sql;
        END;

        SET @sql = N'CREATE FUNCTION ' + @function_q + N' (@unused int)
RETURNS TABLE
WITH SCHEMABINDING
AS
RETURN
    SELECT 1 AS write_allowed
    WHERE ORIGINAL_LOGIN() <> N' + QUOTENAME(@login_name, N'''') + N' COLLATE Latin1_General_CI_AS;';
        EXEC sys.sp_executesql @sql;

        SET @sql = N'CREATE SECURITY POLICY ' + @policy_q + N'
    ADD BLOCK PREDICATE ' + @function_q + N'(1) ON ' + @table_q + N' AFTER INSERT,
    ADD BLOCK PREDICATE ' + @function_q + N'(1) ON ' + @table_q + N' AFTER UPDATE,
    ADD BLOCK PREDICATE ' + @function_q + N'(1) ON ' + @table_q + N' BEFORE UPDATE,
    ADD BLOCK PREDICATE ' + @function_q + N'(1) ON ' + @table_q + N' BEFORE DELETE
    WITH (STATE = ON, SCHEMABINDING = ON);';
        EXEC sys.sp_executesql @sql;

        -- The chatbot must not be able to switch the policy off or change the function.
        SET @sql = N'DENY ALTER ANY SECURITY POLICY TO ' + @role_q + N';';
        EXEC sys.sp_executesql @sql;
        SET @sql = N'DENY ALTER, TAKE OWNERSHIP ON SCHEMA::' + @security_q + N' TO ' + @role_q + N';';
        EXEC sys.sp_executesql @sql;

        -- Read-only visibility for scripts/check_readonly_access.py: without VIEW
        -- DEFINITION the policy is invisible to the chatbot's login, and SELECT on
        -- the function lets it ask "am I blocked?" without writing anything.
        SET @sql = N'GRANT VIEW DEFINITION ON SCHEMA::' + @security_q + N' TO ' + @role_q + N';';
        EXEC sys.sp_executesql @sql;
        SET @sql = N'GRANT SELECT ON OBJECT::' + @function_q + N' TO ' + @role_q + N';';
        EXEC sys.sp_executesql @sql;

        COMMIT TRANSACTION;
        PRINT N'Part 2 applied: ' + @policy_q + N' blocks INSERT, UPDATE and DELETE on ' + @table_q
            + N' for login ' + QUOTENAME(@login_name) + N'.';
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
        PRINT N'Part 2 FAILED and was rolled back: ' + ERROR_MESSAGE();
        THROW;
    END CATCH;
END
ELSE IF @rls_supported = 0
    PRINT N'Part 2 skipped: Row-Level Security needs SQL Server 2016 (13.x) or later; this server is version '
        + CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) + N'. Part 1 still applies.';
ELSE
    PRINT N'Part 2 skipped (report only).';


/* ============================================================================
   PART 3 - Verification. Read-only queries; nothing here changes anything.

   There is deliberately NO test INSERT / UPDATE / DELETE in this file: someone
   highlighting and running a single line would change production data. The
   real end-to-end test is, on the chatbot machine:
       python scripts\check_readonly_access.py
   which connects AS the chatbot login and asks the predicate whether it is
   blocked - without writing anything.
   ============================================================================ */

PRINT N'';
PRINT N'Part 3 - result sets: 3a (expect can_select = 1, every other can_* = 0),'
    + N' 3b (expect 4 BLOCK rows, is_enabled = 1).';

-- 3a. Effective permissions, evaluated as the chatbot. EXECUTE AS LOGIN needs
--     IMPERSONATE on the login (sysadmin has it); a db_owner falls back to the
--     database user, which shows the same table and database permissions.
BEGIN TRY
    EXECUTE AS LOGIN = @login_name;
    SET @impersonated = N'LOGIN';
END TRY
BEGIN CATCH
    PRINT N'3a: EXECUTE AS LOGIN failed (' + ERROR_MESSAGE() + N') - trying the database user.';
END CATCH;

IF @impersonated IS NULL
BEGIN
    BEGIN TRY
        EXECUTE AS USER = @user_name;
        SET @impersonated = N'USER';
    END TRY
    BEGIN CATCH
        PRINT N'3a skipped: could not impersonate ' + @user_q + N': ' + ERROR_MESSAGE();
    END CATCH;
END;

IF @impersonated IS NOT NULL
BEGIN
    BEGIN TRY
        SELECT
            N'3a' AS [check],
            @impersonated AS evaluated_as,
            SUSER_SNAME() AS login_name,
            USER_NAME() AS database_user,
            HAS_PERMS_BY_NAME(@table_q, N'OBJECT', N'SELECT') AS can_select,
            HAS_PERMS_BY_NAME(@table_q, N'OBJECT', N'INSERT') AS can_insert,
            HAS_PERMS_BY_NAME(@table_q, N'OBJECT', N'UPDATE') AS can_update,
            HAS_PERMS_BY_NAME(@table_q, N'OBJECT', N'DELETE') AS can_delete,
            HAS_PERMS_BY_NAME(@table_q, N'OBJECT', N'ALTER') AS can_alter,
            HAS_PERMS_BY_NAME(DB_NAME(), N'DATABASE', N'CREATE TABLE') AS can_create_table;
    END TRY
    BEGIN CATCH
        PRINT N'3a failed: ' + ERROR_MESSAGE();
    END CATCH;
    REVERT;
END;

-- 3b. The security policy on the table. Dynamic SQL, so this batch still
--     compiles on versions without sys.security_policies.
IF @rls_supported = 1
BEGIN
    SET @sql = N'SELECT N''3b'' AS [check],
       OBJECT_SCHEMA_NAME(p.object_id) + N''.'' + p.name AS policy_name,
       p.is_enabled,
       p.is_schema_bound,
       pr.predicate_type_desc,
       pr.operation_desc,
       pr.predicate_definition
FROM sys.security_policies AS p
JOIN sys.security_predicates AS pr ON pr.object_id = p.object_id
WHERE pr.target_object_id = OBJECT_ID(@t)
ORDER BY policy_name, pr.operation;';
    EXEC sys.sp_executesql @sql, N'@t nvarchar(300)', @t = @table_q;
END;

PRINT N'';
IF @problems > 0
    PRINT N'DONE with ' + CAST(@problems AS nvarchar(10))
        + N' WARNING(S) above. Until they are fixed the login is NOT reliably read-only.';
ELSE
    PRINT N'DONE. No membership problems found.';


/* ============================================================================
   ROLLBACK - kept as comments so it can never run by accident.
   Uses the default names; adjust them if you changed the variables above.
   ----------------------------------------------------------------------------

   -- Switch the write block off but keep it (instant, and reversible with STATE = ON):
   ALTER SECURITY POLICY [usgi_security].[chatbot_readonly_policy] WITH (STATE = OFF);

   -- Remove everything, in this order:
   DROP SECURITY POLICY [usgi_security].[chatbot_readonly_policy];
   DROP FUNCTION [usgi_security].[fn_chatbot_write_block];
   DROP SCHEMA [usgi_security];
   ALTER ROLE [usgi_chatbot_readonly] DROP MEMBER [usgi_chatbot];
   DROP ROLE [usgi_chatbot_readonly];   -- its GRANTs and DENYs go with it
   ============================================================================ */
