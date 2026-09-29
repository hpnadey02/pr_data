"""Build / refresh backend/knowledge/column_aliases.json - the column shortcut database.

THE BUSINESS GLOSSARY BELOW IS THE SOURCE OF TRUTH. One block per column, carrying
every way a business user says it plus what it actually means:

    "USGI_SUM_INSURED": {
        "aliases": ["sum insured", "si", "insured amount", "coverage amount"],
        "meaning": "Amount for which the risk is insured under the policy - USGI's share.",
    },

Those two fields go to two different places at runtime, and both matter:

  * `aliases`  -> backend/core/column_registry.py resolves the user's words to the real
                  physical column, so "coverage amount" becomes [USGI_SUM_INSURED] in the
                  generated SQL.
  * `meaning`  -> printed next to the column in the SQL-generation prompt AND embedded in
                  ChromaDB, so the model can tell two similar columns apart.

TO ADD A NEW COLUMN LATER: add one block to GLOSSARY, then run

    python scripts/build_column_aliases.py --rebuild
    python scripts/setup_chromadb.py

Nothing else needs editing. A block naming a column that is not in the live table is
reported as an error rather than silently ignored, so a typo cannot go unnoticed.

Guarantees enforced here so they never have to be discovered at runtime:
  * every live column appears exactly once
  * no shortcut is claimed by two different columns, unless PREFERRED names the winner
  * a shortcut may never shadow a different column's real name
  * every GLOSSARY key matches a real column

Usage:
    python scripts/build_column_aliases.py            # merge: keeps hand edits in the JSON
    python scripts/build_column_aliases.py --rebuild  # GLOSSARY wins; hand edits discarded
    python scripts/build_column_aliases.py --check    # verify only, change nothing
    python scripts/build_column_aliases.py --show "coverage amount"

Default runs UNION the GLOSSARY aliases with whatever is already in the JSON, so a shortcut
typed straight into the JSON survives. `--rebuild` takes the GLOSSARY verbatim, which is how
an alias gets REMOVED.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.column_registry import infer_category  # noqa: E402
from backend.core.identifiers import compact, readable, variant_keys  # noqa: E402
from config.settings import get_settings  # noqa: E402

settings = get_settings()

# --------------------------------------------------------------------------------------
# THE BUSINESS GLOSSARY
#
# Keys are matched to live columns by COMPACTED name, so the exact underscore/case style
# used here does not matter - but the words must match a real column.
#
# Keep every alias unique across columns; --check names any clash. An alias that is also
# another column's real name is rejected: "gross premium" can only ever mean
# [GROSS_PREMIUM], never [USGI_GROSS_PREMIUM], no matter how the business says it.
# --------------------------------------------------------------------------------------
GLOSSARY: dict[str, dict[str, object]] = {
    # -- proposal / policy identity ----------------------------------------------------
    "REFERENCE_NUMBER": {
        "aliases": ["proposal number", "proposal no", "proposal id", "proposal",
                    "reference no", "ref number", "ref no", "refno", "reference id"],
        "meaning": "Proposal number associated with the insurance proposal.",
    },
    "REFERENCE_DATE": {
        "aliases": ["proposal date", "proposal creation date", "ref date", "reference dt"],
        "meaning": "Date of the insurance proposal / reference.",
    },
    "COVERNOTE_NO": {
        "aliases": ["cover note", "cover note no", "covernote", "cn no"],
        "meaning": "Cover note number issued ahead of the policy document.",
    },
    "POLICY_NO": {
        "aliases": ["policy number", "policy no", "pol no", "pol number", "policyno"],
        "meaning": "Numeric policy number. POLICY_NO_CHAR holds the same policy in "
                   "formatted text form; USGIpos_Policy_Number is the POS system's own number.",
    },
    "POLICY_NO_CHAR": {
        "aliases": ["actual policy number", "policy id", "formatted policy number",
                    "policy no char", "policy char", "policy number char", "char policy no"],
        "meaning": "Actual policy number as issued to the customer, formatted text, "
                   "e.g. 2316/84507832/00/000. Show THIS to a human; POLICY_NO is the numeric key.",
    },
    "ENDORSEMENT_NO": {
        "aliases": ["endorsement number", "endt no", "endo no", "endorsement",
                    "policy revision number", "revision number", "policy change number"],
        "meaning": "Number identifying a policy endorsement, revision or change.",
    },
    "Certificate_No": {
        "aliases": ["certificate number", "cert no", "certificate", "group certificate number"],
        "meaning": "Certificate number under a group policy. NULL means no certificate was issued.",
    },
    "Master_Policy": {
        "aliases": ["master policy no", "master pol", "master policy number"],
        "meaning": "Reference number of the master policy this record sits under.",
    },
    "USGIpos_Policy_Number": {
        "aliases": ["usgipos policy", "pos policy number", "usgi pos policy no"],
        "meaning": "POS-system policy number, e.g. AVO/2316/20138002. Not the same as POLICY_NO.",
    },
    "Previous_Yr_Policy": {
        "aliases": ["previous policy", "prev year policy", "last policy",
                    "previous year policy", "previous policy number", "last year policy number",
                    "previous policy no"],
        "meaning": "Policy number covering the previous year / previous insurance period.",
    },
    "Previous_Yr_Insurer": {
        "aliases": ["previous insurer", "prev year insurer", "last insurer",
                    "previous year insurer", "last year insurer", "previous insurance company"],
        "meaning": "Insurance company that provided cover in the previous year.",
    },

    # -- customer ----------------------------------------------------------------------
    "INSURED_ID": {
        "aliases": ["insured identifier", "customer id", "client id", "user id",
                    "policyholder id"],
        "meaning": "Unique identifier of the insured customer.",
    },
    "INSURED_NAME": {
        "aliases": ["customer name", "client name", "insured", "policy holder", "policyholder",
                    "policyholder name"],
        "meaning": "Name of the insured person or entity.",
    },
    "IND_CORP_FLAG": {
        "aliases": ["individual corporate", "ind corp", "customer type", "customer category",
                    "customer segment", "individual corporate flag"],
        "meaning": "Customer type flag: I = Individual, C = Corporate.",
    },
    "TXT_SECTOR": {
        "aliases": ["sector", "industry sector", "business sector", "customer sector", "industry"],
        "meaning": "Sector or industry category of the customer / business.",
    },
    "Customer_State": {
        "aliases": ["client state", "insured state", "customer location state", "policyholder state"],
        "meaning": "State of the customer / insured entity.",
    },
    "Customer_State_Code": {
        "aliases": ["customer state cd", "client state code", "customer state id",
                    "state code of customer"],
        "meaning": "State code of the customer / insured entity.",
    },
    "Customer_Gstin": {
        "aliases": ["customer gst number", "client gstin", "customer tax id", "gstin of customer"],
        "meaning": "GST Identification Number (GSTIN) of the customer.",
    },

    # -- dates -------------------------------------------------------------------------
    "POLICY_ISSUE_DATE": {
        "aliases": ["issue date", "policy date", "issued on", "date of issue",
                    "policy issued date", "issuance date"],
        "meaning": "Date on which the policy was issued. DEFAULT date column for weekly / "
                   "monthly / yearly trend questions.",
    },
    "START_DATE": {
        "aliases": ["risk start date", "inception date", "policy start", "policy start date",
                    "coverage start date"],
        "meaning": "Date from which the policy coverage starts.",
    },
    "EXPIRY_DATE": {
        "aliases": ["end date", "policy expiry", "expiry", "valid till", "policy expiry date",
                    "policy end date", "coverage end date"],
        "meaning": "Date on which the policy coverage expires.",
    },
    "ENDORSEMENT_DATE": {
        "aliases": ["endt date", "endorsement dt", "revision date", "policy change date"],
        "meaning": "Date on which an endorsement or revision took effect.",
    },
    "POLICY_ISSUE_TIME": {
        "aliases": ["issue time", "policy time", "policy issuance time", "issuance time"],
        "meaning": "Time of day at which the policy was issued.",
    },
    "CDC_TIMESTAMP": {
        "aliases": ["cdc time", "record timestamp", "load timestamp",
                    "change data capture timestamp", "data change timestamp",
                    "record change timestamp", "update timestamp"],
        "meaning": "Change-data-capture timestamp of the database record, not a business date.",
    },

    # -- premium and sum insured -------------------------------------------------------
    "GROSS_PREMIUM": {
        "aliases": ["gross prem", "premium", "business", "gwp", "sales",
                    "written premium", "gross written premium"],
        "meaning": "Gross written premium. DEFAULT measure for business / sales / performance "
                   "questions.",
    },
    "NET_PREMIUM": {
        "aliases": ["net prem", "premium net"],
        "meaning": "Premium net of taxes, total across all co-insurers. "
                   "USGI_NET_PREMIUM is USGI's own share.",
    },
    "TOTAL_SUM_INSURED": {
        "aliases": ["total si", "tsi"],
        "meaning": "Total sum insured across all co-insurers. A plain 'sum insured' question "
                   "means USGI_SUM_INSURED, which is USGI's own share.",
    },
    "USGI_GROSS_PREMIUM": {
        "aliases": ["usgi gross prem", "usgi premium", "usgi business", "usgi gwp"],
        "meaning": "Gross premium attributable to USGI. Use ONLY when the question says USGI "
                   "share; a plain 'gross premium' question means GROSS_PREMIUM.",
    },
    "USGI_NET_PREMIUM": {
        "aliases": ["usgi net prem"],
        "meaning": "Net premium attributable to USGI.",
    },
    "USGI_SUM_INSURED": {
        "aliases": ["sum insured", "si", "insured amount", "coverage amount",
                    "usgi si", "usgi sum"],
        "meaning": "Amount for which the risk is insured under the policy - USGI's share. "
                   "DEFAULT column for 'sum insured' / 'SI' questions.",
    },
    "COLLECTED_AMOUNT": {
        "aliases": ["collection amount", "amount collected", "collected", "premium collected"],
        "meaning": "Amount actually collected against the policy or transaction.",
    },
    "Balance_Amount": {
        "aliases": ["balance", "outstanding amount", "due amount", "pending amount",
                    "remaining amount", "unpaid amount"],
        "meaning": "Outstanding amount still payable against the policy or transaction.",
    },
    "LOADING_ON_PREMIUM": {
        "aliases": ["loading", "premium loading", "loading amount", "additional premium loading"],
        "meaning": "Amount loaded onto the premium for risk or pricing factors.",
    },
    "DISCOUNT_ON_PREMIUM": {
        "aliases": ["discount", "premium discount", "discount amount", "discount given"],
        "meaning": "Discount deducted from the applicable premium.",
    },
    "PML": {
        "aliases": ["probable maximum loss", "maximum probable loss", "pml amount"],
        "meaning": "Probable Maximum Loss on the insured risk.",
    },
    "NCB": {
        "aliases": ["no claim bonus", "no claim", "no claim bonus benefit"],
        "meaning": "No Claim Bonus indicator for a customer with no or few claims.",
    },

    # -- motor premium split -----------------------------------------------------------
    "TOTAL_TP_PREMIUM": {
        "aliases": ["tp premium", "third party premium", "total third party"],
        "meaning": "Total Third Party (TP) premium - motor liability cover.",
    },
    "NET_TP_PREMIUM": {
        "aliases": ["net tp prem", "net third party premium"],
        "meaning": "Net premium for Third Party (TP) cover, mainly motor insurance.",
    },
    "TOTAL_OD_PREMIUM": {
        "aliases": ["od premium", "own damage premium", "total own damage"],
        "meaning": "Total Own Damage (OD) premium.",
    },
    "NET_OD_PREMIUM": {
        "aliases": ["net od prem", "net own damage premium"],
        "meaning": "Net premium for Own Damage (OD) cover.",
    },

    # -- terrorism ---------------------------------------------------------------------
    "TERRORISM_PREMIUM": {
        "aliases": ["terrorism prem", "terror premium"],
        "meaning": "Premium for terrorism coverage.",
    },
    "TERRORISM_SHARE": {
        "aliases": ["terror share", "usgi terrorism share"],
        "meaning": "USGI's share of the terrorism premium.",
    },
    "TERRORISM_POOL_PREMIUM": {
        "aliases": ["terror pool premium", "terror pool", "terrorism pool amount"],
        "meaning": "Premium ceded to the terrorism insurance pool.",
    },
    "TERRORISM_POOL_COMMISSION": {
        "aliases": ["terror pool commission", "terrorism commission"],
        "meaning": "Commission received on the terrorism pool cession.",
    },

    # -- reinsurance cessions ----------------------------------------------------------
    "OBLIGATORY_PREMIUM": {
        "aliases": ["obligatory prem"],
        "meaning": "Premium ceded under the obligatory reinsurance treaty.",
    },
    "FACULTATIVE_PREMIUM": {
        "aliases": ["fac premium", "facultative prem"],
        "meaning": "Premium ceded under facultative reinsurance.",
    },
    "SURPLUS_PREMIUM": {
        "aliases": ["surplus prem"],
        "meaning": "Premium ceded under the surplus reinsurance treaty.",
    },
    "MARKET_SURPLUS_PREMIUM": {
        "aliases": ["market surplus prem"],
        "meaning": "Premium ceded to the market surplus treaty.",
    },
    "MOTOR_POOL_PREMIUM": {
        "aliases": ["motor pool prem"],
        "meaning": "Premium ceded to the motor pool.",
    },
    "Quota_Share_Premium": {
        "aliases": ["quota share prem", "qs premium"],
        "meaning": "Premium ceded under the quota share treaty.",
    },
    "OBLIGATORY_COMMISSION": {
        "aliases": ["obligatory comm"],
        "meaning": "Commission received on obligatory reinsurance cessions.",
    },
    "FACULTATIVE_COMMISSION": {
        "aliases": ["fac commission", "facultative comm"],
        "meaning": "Commission received on facultative cessions.",
    },
    "SURPLUS_COMMISSION": {
        "aliases": ["surplus comm"],
        "meaning": "Commission received on surplus treaty cessions.",
    },
    "MARKET_SURPLUS_COMMISSION": {
        "aliases": ["market surplus comm"],
        "meaning": "Commission received on market surplus cessions.",
    },
    "MOTOR_POOL_COMMISSION": {
        "aliases": ["motor pool comm"],
        "meaning": "Commission received on motor pool cessions.",
    },
    "Quota_Share_Commission": {
        "aliases": ["quota share comm", "qs commission"],
        "meaning": "Commission received on quota share cessions.",
    },

    # -- commission --------------------------------------------------------------------
    "COMMISSION_PER": {
        "aliases": ["commission percentage", "commission pct", "comm percent", "commission rate",
                    "agent commission percentage", "broker commission percentage"],
        "meaning": "Commission RATE (a percentage) on the policy. Never SUM this - average it.",
    },
    "COMMISSION_AMOUNT": {
        "aliases": ["commission amt", "comm amount", "brokerage", "commission value",
                    "agent commission", "broker commission", "commission earned"],
        "meaning": "Commission AMOUNT payable on the policy or transaction.",
    },

    # -- tax -------------------------------------------------------------------------
    "TOTAL_SERVICE_TAX": {
        "aliases": ["service tax", "total tax"],
        "meaning": "Total service tax / applicable tax on the premium.",
    },
    "USGI_SERVICE_TAX_SHARE": {
        "aliases": ["usgi service tax", "usgi tax share", "service tax share"],
        "meaning": "USGI's share of the service tax component.",
    },
    "STAMP_DUTY": {
        "aliases": ["stamp", "stamp charges", "stamp duty amount"],
        "meaning": "Stamp duty on the insurance transaction.",
    },
    "Sgst_Percentage": {
        "aliases": ["sgst percent", "sgst pct", "sgst rate", "state gst percentage"],
        "meaning": "State GST (SGST) rate applied to the transaction.",
    },
    "Sgst_Net_Amount": {
        "aliases": ["sgst amount", "sgst amt", "state gst amount", "sgst value"],
        "meaning": "State GST (SGST) amount.",
    },
    "Cgst_Percentage": {
        "aliases": ["cgst percent", "cgst pct", "cgst rate", "central gst percentage"],
        "meaning": "Central GST (CGST) rate applied to the transaction.",
    },
    "Cgst_Net_Amount": {
        "aliases": ["cgst amount", "cgst amt", "central gst amount", "cgst value"],
        "meaning": "Central GST (CGST) amount.",
    },
    "Igst_Percentage": {
        "aliases": ["igst percent", "igst pct", "igst rate", "integrated gst percentage"],
        "meaning": "Integrated GST (IGST) rate applied to the transaction.",
    },
    "Igst_Net_Amount": {
        "aliases": ["igst amount", "igst amt", "integrated gst amount", "igst value"],
        "meaning": "Integrated GST (IGST) amount.",
    },
    "Total_Gst": {
        "aliases": ["gst", "total tax gst", "total gst amount", "gst total", "total tax amount"],
        "meaning": "Total GST amount on the transaction (SGST + CGST + IGST).",
    },
    "Txt_Type_Of_Tax": {
        "aliases": ["type of tax", "tax type"],
        "meaning": "Which tax regime applies to the transaction.",
    },
    "K_Cess_Amount": {
        "aliases": ["cess amount", "k cess", "krishi cess"],
        "meaning": "Krishi Kalyan cess amount.",
    },
    "Num_Cess_Percentage": {
        "aliases": ["cess percentage", "cess pct", "cess rate"],
        "meaning": "Cess rate applied to the transaction.",
    },

    # -- distribution: intermediary, BA, sales managers --------------------------------
    "INTERMEDIARY": {
        "aliases": ["agent", "broker", "channel partner", "intermediary name",
                    "agent name", "broker name"],
        "meaning": "Name of the agent / broker who sourced the policy. Primary dimension for "
                   "intermediary performance questions.",
    },
    "INTERMEDIARY_CODE": {
        "aliases": ["agent code", "broker code", "intermediary cd", "agent id", "broker id"],
        "meaning": "Code of the intermediary / agent / broker who sold the policy.",
    },
    "INTERMEDIARY_CATEGORY": {
        "aliases": ["agent category", "broker category", "intermediary type", "channel category"],
        "meaning": "Classification of the intermediary - organisation vs individual, and so on.",
    },
    "Intermediary_Gstin": {
        "aliases": ["agent gstin", "broker gstin", "intermediary gst number", "agent gst number"],
        "meaning": "GST Identification Number (GSTIN) of the intermediary.",
    },
    "SUM_IMD_CODE": {
        "aliases": ["imd code", "imd", "enterprise code", "institution code",
                    "organization code", "channel enterprise code"],
        "meaning": "Code of the enterprise or organisation the policy came through - a bank, "
                   "an OEM, and so on.",
    },
    "BA_NAME": {
        "aliases": ["business associate", "ba", "business associate name",
                    "relationship manager name", "rm name"],
        "meaning": "Relationship manager / business associate handling the business.",
    },
    "Ba_Code": {
        "aliases": ["business associate code", "ba cd", "relationship manager code", "rm code"],
        "meaning": "Employee code of the relationship manager / business associate.",
    },
    "Primary_Sales_Manager_Name": {
        "aliases": ["primary sales manager", "sales manager", "primary rm", "primary sm",
                    "primary salesperson"],
        "meaning": "Primary sales manager on the policy.",
    },
    "Primary_Sales_Manager_Code": {
        "aliases": ["primary sales manager cd", "primary rm code", "primary sm code",
                    "primary sales employee code"],
        "meaning": "Employee code of the primary sales manager.",
    },
    "Secondary_Sales_Manager_Name": {
        "aliases": ["secondary sales manager", "secondary rm", "secondary sm",
                    "secondary salesperson"],
        "meaning": "Secondary sales manager on the policy.",
    },
    "Secondary_Sales_Manager_Code": {
        "aliases": ["secondary sales manager cd", "secondary rm code", "secondary sm code",
                    "secondary sales employee code"],
        "meaning": "Employee code of the secondary sales manager.",
    },
    "Tertiary_Sales_Manager_Name": {
        "aliases": ["tertiary sales manager", "tertiary rm", "tertiary sm",
                    "tertiary salesperson"],
        "meaning": "Tertiary sales manager on the policy.",
    },
    "Tertiary_Sales_Manager_Code": {
        "aliases": ["tertiary sales manager cd", "tertiary rm code", "tertiary sm code",
                    "tertiary sales employee code"],
        "meaning": "Employee code of the tertiary sales manager.",
    },
    "Employee_Code": {
        "aliases": ["employee id", "emp code", "staff code", "sales employee code",
                    "employee number"],
        "meaning": "Code of the employee who sold or handled the policy.",
    },
    "Subvertical_Channel": {
        "aliases": ["sub vertical", "subvertical", "sub channel", "sub vertical channel",
                    "channel subcategory"],
        "meaning": "Sub-channel the policy was sold through, one level below the vertical.",
    },
    "Vertical_Channel_Map": {
        "aliases": ["vertical", "channel", "vertical wise", "vertical channel",
                    "channel mapping", "sales channel", "business channel", "vertical mapping"],
        "meaning": "Distribution vertical / sales channel. Primary dimension for vertical-wise "
                   "questions.",
    },

    # -- USGI organisation -------------------------------------------------------------
    "BRANCH_NAME": {
        "aliases": ["branch", "branch office", "high performing branch", "top branch",
                    "associated branch", "organization name"],
        "meaning": "Branch name on the policy. Primary dimension for branch-wise / region-wise "
                   "performance. May also hold a company, organisation or person name.",
    },
    "BRANCH_OFFICE_CODE": {
        "aliases": ["branch code", "office code", "branch office cd", "usgi branch code"],
        "meaning": "Code of the USGI branch / office.",
    },
    "OFFICE_NAME": {
        "aliases": ["office", "usgi office", "usgi branch office", "issuing office"],
        "meaning": "Name of the USGI office that issued the policy.",
    },
    "Usgi_Branch_State": {
        "aliases": ["branch state", "servicing branch state", "usgi branch state",
                    "usgi office state", "usgi state"],
        "meaning": "State the servicing USGI branch sits in.",
    },
    "Usgi_Branch_State_Code": {
        "aliases": ["usgi branch state cd", "branch state code", "usgi state code"],
        "meaning": "State code of the servicing USGI branch.",
    },
    "DEPARTMENT_CODE": {
        "aliases": ["dept code", "department", "business department code"],
        "meaning": "Insurance department / business category code, e.g. 21 Fire, 22 Engineering, "
                   "23 Motor.",
    },
    "COMPANY_ID": {
        "aliases": ["company identifier"],
        "meaning": "Internal company identifier.",
    },
    "COMPANY_SHORT_DESCRIPTION": {
        "aliases": ["company short desc", "company description", "company name"],
        "meaning": "Short description / name of the company on the record.",
    },
    "User_Name": {
        "aliases": ["user", "created by", "entered by", "username", "application user",
                    "login user", "system user"],
        "meaning": "Application user who created the record.",
    },

    # -- product -----------------------------------------------------------------------
    "LINE_OF_BUSINESS": {
        "aliases": ["lob", "business line", "line of biz", "insurance business type"],
        "meaning": "Class of insurance business - Fire, Engineering, Motor, and so on.",
    },
    "PRODUCT_CODE": {
        "aliases": ["product cd", "insurance product code", "policy product code"],
        "meaning": "Code of the insurance product issued.",
    },
    "PRODUCT_NAME": {
        "aliases": ["product", "product wise", "plan name", "insurance product",
                    "policy product", "insurance plan"],
        "meaning": "Name of the insurance product. Primary dimension for product-wise questions.",
    },
    "Business_Type_Fresh_Renewal": {
        "aliases": ["business type", "fresh renewal", "new or renewal", "renewal flag",
                    "policy business type", "policy type", "fresh renewal endorsement"],
        "meaning": "Whether the policy business is Fresh, Renewal or Endorsement. Dimension for "
                   "business-type questions.",
    },

    # -- coinsurance -------------------------------------------------------------------
    "COINSURANCE_CATEGORY": {
        "aliases": ["coinsurance cat", "co insurance category", "incoming outgoing coinsurance"],
        "meaning": "Incoming means USGI is the smaller participating partner; Outgoing means "
                   "USGI holds the major share.",
    },
    "COINSURANCE_TYPE": {
        "aliases": ["co insurance type", "coins type"],
        "meaning": "Type of the coinsurance arrangement.",
    },
    "SHARE_PERCENTAGE": {
        "aliases": ["share percent", "share pct", "share %", "usgi share",
                    "participation percentage"],
        "meaning": "USGI's percentage share in the policy / coinsurance arrangement. "
                   "Never SUM this - average it.",
    },
    "LEADER_NONLEADER": {
        "aliases": ["leader non leader", "leader flag", "leader status",
                    "coinsurance leadership status"],
        "meaning": "Whether USGI leads the coinsurance arrangement or follows it.",
    },

    # -- motor asset -------------------------------------------------------------------
    "MAKE": {
        "aliases": ["vehicle make", "manufacturer", "brand", "equipment make",
                    "product manufacturer"],
        "meaning": "Manufacturer or brand of the insured vehicle / equipment.",
    },
    "MODEL": {
        "aliases": ["vehicle model", "equipment model", "product model"],
        "meaning": "Model of the insured vehicle / equipment.",
    },
    "VARIENT": {
        "aliases": ["variant", "vehicle variant", "trim", "product variant",
                    "equipment variant", "version"],
        "meaning": "Variant / version of the insured vehicle or equipment. The column name is "
                   "spelt VARIENT in the table.",
    },
    "YEAR_OF_MANUFACTURING": {
        "aliases": ["manufacturing year", "year of mfg", "mfg year", "model year",
                    "year of manufacture", "yom", "manufacture year"],
        "meaning": "Year the insured vehicle / equipment was manufactured.",
    },
    "MTOR_Registration_No": {
        "aliases": ["registration number", "vehicle registration", "reg no", "rc number",
                    "motor registration number", "vehicle registration number", "vehicle number",
                    "registration no"],
        "meaning": "Registration number of the insured motor vehicle.",
    },
    "RTO_Location": {
        "aliases": ["rto", "rto office", "registration office", "regional transport office",
                    "rto area"],
        "meaning": "Regional Transport Office the vehicle is registered at.",
    },
    "Gvw": {
        "aliases": ["gross vehicle weight", "vehicle weight"],
        "meaning": "Gross vehicle weight of the insured vehicle.",
    },
    "Vehicle_Seating_Capacity": {
        "aliases": ["seating capacity", "seats", "vehicle seats", "number of seats",
                    "seat capacity", "no of seats"],
        "meaning": "Seating capacity of the insured vehicle.",
    },

    # -- geography ---------------------------------------------------------------------
    "STATE": {
        "aliases": ["zone", "region", "geography", "state wise", "policy state",
                    "product state", "manufacturing state"],
        "meaning": "State where the risk is located. Proxy for zone / region questions.",
    },

    # -- banking / accounting ----------------------------------------------------------
    "VOUCHER_NO": {
        "aliases": ["voucher number", "vch no", "banking voucher number"],
        "meaning": "Voucher number of the banking / accounting entry.",
    },
    "VOUCHER_DATE": {
        "aliases": ["vch date"],
        "meaning": "Date of the banking / accounting voucher.",
    },
    "Invoice_Number": {
        "aliases": ["invoice no", "inv no", "bill number", "invoice id"],
        "meaning": "Invoice number raised for the policy or transaction.",
    },
    "Payment_Id": {
        "aliases": ["payment identifier", "payment reference", "payment number",
                    "transaction id", "payment reference id"],
        "meaning": "Identifier of the payment transaction.",
    },
    "DEPOSIT_SLIP_NUMBER": {
        "aliases": ["deposit slip", "deposit no"],
        "meaning": "Bank deposit slip number for the collection.",
    },
    "LOAN_ACCOUNT_NUMBER": {
        "aliases": ["loan account", "loan ac no", "loan no", "loan account number", "loan id"],
        "meaning": "Loan account the insurance was taken against.",
    },
    "Advpremcollremarks": {
        "aliases": ["advance premium collection remarks", "adv prem remarks",
                    "advance premium collection", "advance premium remarks",
                    "multi year advance premium"],
        "meaning": "Remarks on advance premium collection, typically for multi-year policies.",
    },

    # -- operational -------------------------------------------------------------------
    "Inward_Number": {
        "aliases": ["inward no", "inward"],
        "meaning": "Inward reference number of the document.",
    },
    "Sub_Inward_Number": {
        "aliases": ["sub inward number", "sub inward no", "sub inward", "subinward no"],
        "meaning": "Sub-inward reference number under the inward number.",
    },
    "Live_Count": {
        "aliases": ["live policies", "active count"],
        "meaning": "Live / active policy indicator.",
    },
    "Mi_Remarks": {
        "aliases": ["mi remark", "management remarks"],
        "meaning": "Management information remarks on the record.",
    },
}

# --------------------------------------------------------------------------------------
# Deliberate tie-breaks.
#
# A shortcut claimed by two columns is normally DROPPED - silently picking one is how a
# chatbot returns a confidently wrong number. These are the cases where the business has
# actually decided which column it means, so the winner is recorded here instead.
#
# Every entry needs a reason. If you cannot write the reason, the phrase is ambiguous and
# belongs nowhere near this map.
# --------------------------------------------------------------------------------------
PREFERRED: dict[str, str] = {
    # The business glossary defines "sum insured" as USGI's share, not the co-insurance total.
    "sum insured": "USGI_SUM_INSURED",
    "si": "USGI_SUM_INSURED",
    "insured amount": "USGI_SUM_INSURED",
    "coverage amount": "USGI_SUM_INSURED",
    # "IMD" is the enterprise/bank code in this business, not the intermediary name.
    "imd": "SUM_IMD_CODE",
    "imd code": "SUM_IMD_CODE",
}


def _load_existing() -> dict:
    path = settings.resolved(settings.COLUMN_ALIASES_FILE)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle).get("columns", {}) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _live_columns() -> list[dict]:
    from backend.core.datasource import get_datasource

    return get_datasource().get_table_columns()


def _glossary_aliases(entry: dict) -> list[str]:
    return [str(a) for a in (entry.get("aliases") or []) if str(a).strip()]


def build(live: list[dict], rebuild: bool = False) -> tuple[dict, list[str]]:
    """Return (payload, problems). `problems` is empty when the file is sound.

    `rebuild=True` takes the GLOSSARY verbatim; otherwise its aliases are UNIONed with
    whatever is already in the JSON, so a shortcut typed straight into the file survives.
    """
    existing = _load_existing()
    existing_by_key = {compact(name): entry for name, entry in existing.items()}
    glossary_by_key = {compact(name): entry for name, entry in GLOSSARY.items()}

    physical_keys = {compact(c["column_name"]): c["column_name"] for c in live}
    payload: dict[str, dict] = {}
    claims: dict[str, set[str]] = defaultdict(set)
    curated_claims: dict[str, set[str]] = defaultdict(set)

    for column in live:
        name = str(column["column_name"]).strip()
        key = compact(name)
        data_type = str(column.get("data_type") or "varchar")
        prior = existing_by_key.get(key, {})
        glossary = glossary_by_key.get(key, {})

        curated = _glossary_aliases(glossary)
        if rebuild:
            aliases = curated
        else:
            # Hand edits in the JSON are kept alongside the glossary, never instead of it -
            # a new alias added to GLOSSARY must land on the next ordinary run.
            aliases = [*curated, *(str(a) for a in prior.get("aliases", []) if str(a).strip())]
        aliases = sorted({a.strip() for a in aliases if a.strip()})

        generated = sorted(variant_keys(name) - {key})

        for alias in aliases:
            alias_key = compact(alias)
            if alias_key:
                curated_claims[alias_key].add(name)
                claims[alias_key].add(name)
        for alias_key in generated:
            claims[alias_key].add(name)

        # The glossary is the source of truth for meanings, so it OVERRIDES the description
        # already in the JSON - most of those were auto-generated from the column name.
        meaning = str(glossary.get("meaning") or "").strip()
        # --rebuild re-derives the category from the LIVE type. This matters when the file
        # was generated against one data source and is being rebuilt against another: a
        # category frozen from the local CSV would otherwise survive onto SQL Server, where
        # it decides which columns are SUM-able measures and which are date axes.
        category = infer_category(name, data_type) if rebuild else (
            str(prior.get("category") or infer_category(name, data_type))
        )
        payload[name] = {
            "category": category,
            "data_type": data_type,
            "description": meaning or str(prior.get("description") or f"{readable(name)}."),
            "aliases": aliases,
            "generated": generated,
        }

    problems: list[str] = []

    # A glossary block for a column that does not exist is a typo, not a no-op.
    for entry_name in GLOSSARY:
        if compact(entry_name) not in physical_keys:
            problems.append(
                f"GLOSSARY names '{entry_name}', which is not a column of "
                f"{settings.DB_TABLE} - fix the spelling or remove the block"
            )

    preferred_keys: dict[str, str] = {}
    for phrase, winner in PREFERRED.items():
        resolved = physical_keys.get(compact(winner))
        if not resolved:
            problems.append(
                f"PREFERRED sends '{phrase}' to '{winner}', which is not a live column"
            )
            continue
        preferred_keys[compact(phrase)] = resolved

    for alias_key, owners in sorted(curated_claims.items()):
        if len(owners) > 1 and alias_key not in preferred_keys:
            problems.append(
                f"shortcut '{alias_key}' is claimed by {sorted(owners)} - give each column a "
                "distinct shortcut, or name the winner in PREFERRED"
            )
        elif alias_key in physical_keys and physical_keys[alias_key] not in owners:
            problems.append(
                f"shortcut '{alias_key}' shadows the real column "
                f"'{physical_keys[alias_key]}' - a column's own name always wins"
            )

    dropped = {
        alias_key: sorted(owners)
        for alias_key, owners in claims.items()
        if len(owners) > 1 and alias_key not in curated_claims and alias_key not in preferred_keys
    }
    resolved_ties = {
        alias_key: preferred_keys[alias_key]
        for alias_key, owners in claims.items()
        if len(owners) > 1 and alias_key in preferred_keys
    }
    # The reverse of the typo check above: a column the LIVE table has but the glossary does
    # not describe. Not an error - it still resolves by its own name - but on a data source
    # whose schema has moved on, this is the list of columns the chatbot cannot be asked
    # about in business words.
    missing = [
        str(c["column_name"]).strip()
        for c in live
        if compact(str(c["column_name"])) not in glossary_by_key
    ]

    return (
        {
            "columns": payload,
            "_ambiguous_generated": dropped,
            "_preferred": preferred_keys,
            "_resolved_ties": resolved_ties,
            "_not_in_glossary": missing,
        },
        problems,
    )


def write(payload: dict) -> Path:
    path = settings.resolved(settings.COLUMN_ALIASES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "table": settings.DB_TABLE,
        "source": settings.describe_source(),
        "notes": [
            "Add a permanent alias or meaning in GLOSSARY inside "
            "scripts/build_column_aliases.py, then re-run it with --rebuild.",
            "Editing the 'aliases' list here also works and survives an ordinary re-run, "
            "but --rebuild discards it.",
            "'generated' and 'preferred_shortcuts' are rebuilt by the script - "
            "hand edits there are lost.",
            "'description' is the meaning shown to the SQL model and embedded in ChromaDB. "
            "Re-run scripts/setup_chromadb.py after changing it.",
            "Matching ignores case, spaces, underscores and hyphens entirely.",
            "A shortcut claimed by two columns is ignored at runtime unless PREFERRED names "
            "the winner; the build script names every one it dropped.",
        ],
        "columns": payload["columns"],
        "preferred_shortcuts": payload["_preferred"],
        "ambiguous_generated_shortcuts_ignored": payload["_ambiguous_generated"],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate only, write nothing")
    parser.add_argument(
        "--rebuild", action="store_true",
        help="take GLOSSARY verbatim; discard aliases hand-edited into the JSON",
    )
    parser.add_argument("--show", metavar="PHRASE", help="print every shortcut for one column")
    args = parser.parse_args()

    print(f"Data source: {settings.describe_source()}")
    try:
        live = _live_columns()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to read the live schema: {exc}")
        return 1
    print(f"Live columns: {len(live)}   Glossary entries: {len(GLOSSARY)}")

    payload, problems = build(live, rebuild=args.rebuild)

    if args.show:
        from backend.core.column_registry import build_registry

        registry = build_registry(live)
        target = registry.resolve(args.show)
        if not target:
            print(f"'{args.show}' does not resolve to any column. Closest: {registry.suggest(args.show)}")
            return 1
        info = registry.get(target)
        print(f"\n{target}  ({info.category}, {info.data_type})")
        print(f"  meaning   : {info.description}")
        print(f"  aliases   : {info.aliases}")
        print(f"  shortcuts : {registry.shortcuts_for(target)}")
        return 0

    if problems:
        print(f"\n{len(problems)} problem(s) found - the file was NOT written:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    described = sum(1 for e in payload["columns"].values() if e["description"])
    total_shortcuts = sum(
        len(entry["aliases"]) + len(entry["generated"]) for entry in payload["columns"].values()
    )
    print(f"Shortcuts: {total_shortcuts} across {len(payload['columns'])} columns")
    print(f"Meanings : {described}/{len(payload['columns'])} columns described")

    missing = payload["_not_in_glossary"]
    if missing:
        print(
            f"\nLive columns with NO glossary block ({len(missing)}) - these resolve only by "
            f"their own name, with no business aliases and no meaning in the SQL prompt:"
        )
        for name in missing:
            print(f"  - {name}")
        print("  Add a block for each to GLOSSARY, then re-run with --rebuild.\n")

    ties = payload["_resolved_ties"]
    if ties:
        print(f"Ties resolved by PREFERRED: {len(ties)}")
        for key, winner in ties.items():
            print(f"  - '{key}' -> {winner}")
    # A preference nothing contests is doing no work today. Say so, so it is never mistaken
    # for the reason a phrase resolves the way it does.
    idle = sorted(set(payload["_preferred"]) - set(ties))
    if idle:
        print(f"PREFERRED entries nothing currently contests ({len(idle)}): {', '.join(idle)}")

    dropped = payload["_ambiguous_generated"]
    if dropped:
        print(f"Ambiguous auto-generated shortcuts ignored: {len(dropped)}")
        for key, owners in list(dropped.items())[:5]:
            print(f"  - '{key}' -> {owners}")

    if args.check:
        print("\n--check passed: no collisions.")
        return 0

    path = write(payload)
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
