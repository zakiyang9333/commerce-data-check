"""XBRL concept map. Order = priority. Changing this file changes the
caliber for every company, so record any change in CHANGELOG.md."""

CONCEPTS = {
    "us-gaap": {
        "ebit_op": ["OperatingIncomeLoss"],
        "pretax": [
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        ],
        "interest": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt", "InterestAndDebtExpense"],
        "revenue": [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
        ],
        "assets": ["Assets"],
        "liab_cur": ["LiabilitiesCurrent"],
        "assets_cur": ["AssetsCurrent"],
        "goodwill": ["Goodwill"],
        "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
        "lease_liab": ["OperatingLeaseLiability", "OperatingLeaseLiabilityNoncurrent"],
        # one-off items (Adjusted ROCE)
        "impair_total": ["AssetImpairmentCharges"],
        "impair_gw": ["GoodwillImpairmentLoss"],
        "impair_intang": ["ImpairmentOfIntangibleAssetsExcludingGoodwill"],
        "impair_lla": ["ImpairmentOfLongLivedAssetsHeldForUse"],
        "restructuring": ["RestructuringCharges"],
        "disposal_gain": [
            "GainLossOnDispositionOfAssets1",
            "GainLossOnSaleOfPropertyPlantEquipment",
            "DisposalGroupNotDiscontinuedOperationGainLossOnDisposal",
        ],
        "acquisitions": ["PaymentsToAcquireBusinessesNetOfCashAcquired"],
    },
    "ifrs-full": {
        "ebit_op": ["ProfitLossFromOperatingActivities"],
        "pretax": ["ProfitLossBeforeTax"],
        "interest": ["FinanceCosts", "InterestExpense"],
        "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
        "assets": ["Assets"],
        "liab_cur": ["CurrentLiabilities"],
        "assets_cur": ["CurrentAssets"],
        "goodwill": ["Goodwill"],
        "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
        "lease_liab": ["LeaseLiabilities"],
        "impair_total": ["ImpairmentLossRecognisedInProfitOrLoss"],
        "impair_gw": [],
        "impair_intang": [],
        "impair_lla": [],
        "restructuring": [],
        "disposal_gain": ["GainsLossesOnDisposalsOfPropertyPlantAndEquipment"],
        "acquisitions": ["CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities"],
    },
}

# ROCE grade bands from the spec (section 5). Lower bound inclusive.
BANDS = [
    (0.50, "卓越资本回报"),
    (0.40, "极优秀"),
    (0.30, "优秀"),
    (0.20, "良好"),
    (0.15, "观察"),
    (0.10, "一般"),
    (float("-inf"), "低资本回报"),
]

# Data-distortion thresholds (section 8). Our own choices, documented.
NEAR_ZERO_CE_SHARE_OF_ASSETS = 0.10   # avg CE < 10% of avg total assets -> N/M
GOODWILL_HEAVY_SHARE_OF_CE = 0.40
LEASE_HEAVY_SHARE_OF_CE = 0.20
ONE_OFF_SHARE_OF_EBIT = 0.10
ACQUISITION_SHARE_OF_CE = 0.10
CE_JUMP_RATIO = 1.5
STALE_DAYS = 200


def band(roce):
    if roce is None:
        return "N/A"
    for lo, name in BANDS:
        if roce >= lo:
            return name
    return "低资本回报"
