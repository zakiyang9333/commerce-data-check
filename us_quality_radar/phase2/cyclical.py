"""Cyclicality labels (spec section 3). A strong / B medium / C weak / D special.
Assigned by GICS sub-industry (or industry), with radar-segment overrides.
Unknown industry -> "未知" (no guess)."""

A_SUB = {"Oil & Gas Drilling", "Oil & Gas Equipment & Services", "Integrated Oil & Gas", "Oil & Gas Exploration & Production",
         "Oil & Gas Refining & Marketing", "Coal & Consumable Fuels", "Commodity Chemicals", "Diversified Chemicals",
         "Fertilizers & Agricultural Chemicals", "Specialty Chemicals", "Construction Materials", "Aluminum",
         "Diversified Metals & Mining", "Copper", "Gold", "Precious Metals & Minerals", "Silver", "Steel", "Forest Products",
         "Paper Products", "Marine Transportation", "Semiconductor Materials & Equipment"}
A_IND = {"Chemicals", "Metals & Mining", "Paper & Forest Products", "Energy Equipment & Services", "Oil, Gas & Consumable Fuels",
         "Marine Transportation", "Construction Materials"}
B_IND = {"Automobile Components", "Automobiles", "Machinery", "Aerospace & Defense", "Building Products", "Construction & Engineering",
         "Electrical Equipment", "Industrial Conglomerates", "Trading Companies & Distributors", "Passenger Airlines",
         "Air Freight & Logistics", "Ground Transportation", "Transportation Infrastructure", "Household Durables", "Leisure Products",
         "Communications Equipment", "Technology Hardware, Storage & Peripherals", "Electronic Equipment, Instruments & Components",
         "Semiconductors & Semiconductor Equipment", "Containers & Packaging", "Independent Power and Renewable Electricity Producers",
         "Real Estate Management & Development"}
B_SUB = {"Industrial Gases", "Casinos & Gaming", "Hotels, Resorts & Cruise Lines", "Oil & Gas Storage & Transportation",
         "Semiconductors"}
D_SECTORS = {"Financials"}
D_GROUPS = {"Equity Real Estate Investment Trusts (REITs)"}

SEG_A = {"存储芯片", "存储设备（硬盘）", "晶圆代工", "封装测试", "半导体设备", "半导体材料"}
SEG_B = {"GPU", "CPU/GPU", "CPU", "芯片设计", "模拟与功率芯片（IDM）", "AI基础设施"}
SEG_C = {"SaaS", "企业软件", "云计算", "网络安全"}

LABEL = {"A": "强周期", "B": "中等周期", "C": "弱周期", "D": "特殊商业模式", "未知": "未知"}


# payment networks and financial data/ratings are fee businesses, spec puts payment networks in C
C_FIN_SUB = {"Transaction & Payment Processing Services", "Financial Exchanges & Data"}


def cyclical_class(sector, group, industry, sub, segment, phase1_group):
    if sub in C_FIN_SUB and phase1_group == "general":
        return "C"
    if phase1_group in ("financial", "reit") or sector in D_SECTORS or group in D_GROUPS:
        return "D"
    if segment in SEG_A:
        return "A"
    if segment in SEG_B:
        return "B"
    if segment in SEG_C:
        return "C"
    if sub in A_SUB or industry in A_IND:
        return "A"
    if sub in B_SUB or industry in B_IND:
        return "B"
    if sector in (None, "", "Unclassified"):
        return "未知"
    return "C"
