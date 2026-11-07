"""GICS structure (MSCI/S&P, revision effective March 2023), names only.
Sector -> Industry Group -> Industry -> [Sub-Industries]. Used to fill the
parent levels once a sub-industry or industry is known."""
TREE = {
 "Energy": {"Energy": {
   "Energy Equipment & Services": ["Oil & Gas Drilling", "Oil & Gas Equipment & Services"],
   "Oil, Gas & Consumable Fuels": ["Integrated Oil & Gas", "Oil & Gas Exploration & Production", "Oil & Gas Refining & Marketing",
                                   "Oil & Gas Storage & Transportation", "Coal & Consumable Fuels"]}},
 "Materials": {"Materials": {
   "Chemicals": ["Commodity Chemicals", "Diversified Chemicals", "Fertilizers & Agricultural Chemicals", "Industrial Gases", "Specialty Chemicals"],
   "Construction Materials": ["Construction Materials"],
   "Containers & Packaging": ["Metal, Glass & Plastic Containers", "Paper & Plastic Packaging Products & Materials"],
   "Metals & Mining": ["Aluminum", "Diversified Metals & Mining", "Copper", "Gold", "Precious Metals & Minerals", "Silver", "Steel"],
   "Paper & Forest Products": ["Forest Products", "Paper Products"]}},
 "Industrials": {
  "Capital Goods": {
   "Aerospace & Defense": ["Aerospace & Defense"], "Building Products": ["Building Products"],
   "Construction & Engineering": ["Construction & Engineering"],
   "Electrical Equipment": ["Electrical Components & Equipment", "Heavy Electrical Equipment"],
   "Industrial Conglomerates": ["Industrial Conglomerates"],
   "Machinery": ["Construction Machinery & Heavy Transportation Equipment", "Agricultural & Farm Machinery", "Industrial Machinery & Supplies & Components"],
   "Trading Companies & Distributors": ["Trading Companies & Distributors"]},
  "Commercial & Professional Services": {
   "Commercial Services & Supplies": ["Commercial Printing", "Environmental & Facilities Services", "Office Services & Supplies",
                                      "Diversified Support Services", "Security & Alarm Services"],
   "Professional Services": ["Human Resource & Employment Services", "Research & Consulting Services", "Data Processing & Outsourced Services"]},
  "Transportation": {
   "Air Freight & Logistics": ["Air Freight & Logistics"], "Passenger Airlines": ["Passenger Airlines"],
   "Marine Transportation": ["Marine Transportation"],
   "Ground Transportation": ["Rail Transportation", "Cargo Ground Transportation", "Passenger Ground Transportation"],
   "Transportation Infrastructure": ["Airport Services", "Highways & Railtracks", "Marine Ports & Services"]}},
 "Consumer Discretionary": {
  "Automobiles & Components": {
   "Automobile Components": ["Automotive Parts & Equipment", "Tires & Rubber"],
   "Automobiles": ["Automobile Manufacturers", "Motorcycle Manufacturers"]},
  "Consumer Durables & Apparel": {
   "Household Durables": ["Consumer Electronics", "Home Furnishings", "Homebuilding", "Household Appliances", "Housewares & Specialties"],
   "Leisure Products": ["Leisure Products"],
   "Textiles, Apparel & Luxury Goods": ["Apparel, Accessories & Luxury Goods", "Footwear", "Textiles"]},
  "Consumer Services": {
   "Hotels, Restaurants & Leisure": ["Casinos & Gaming", "Hotels, Resorts & Cruise Lines", "Leisure Facilities", "Restaurants"],
   "Diversified Consumer Services": ["Education Services", "Specialized Consumer Services"]},
  "Consumer Discretionary Distribution & Retail": {
   "Distributors": ["Distributors"], "Broadline Retail": ["Broadline Retail"],
   "Specialty Retail": ["Apparel Retail", "Computer & Electronics Retail", "Home Improvement Retail", "Other Specialty Retail",
                        "Automotive Retail", "Homefurnishing Retail"]}},
 "Consumer Staples": {
  "Consumer Staples Distribution & Retail": {
   "Consumer Staples Distribution & Retail": ["Drug Retail", "Food Distributors", "Food Retail", "Consumer Staples Merchandise Retail"]},
  "Food, Beverage & Tobacco": {
   "Beverages": ["Brewers", "Distillers & Vintners", "Soft Drinks & Non-alcoholic Beverages"],
   "Food Products": ["Agricultural Products & Services", "Packaged Foods & Meats"], "Tobacco": ["Tobacco"]},
  "Household & Personal Products": {"Household Products": ["Household Products"], "Personal Care Products": ["Personal Care Products"]}},
 "Health Care": {
  "Health Care Equipment & Services": {
   "Health Care Equipment & Supplies": ["Health Care Equipment", "Health Care Supplies"],
   "Health Care Providers & Services": ["Health Care Distributors", "Health Care Services", "Health Care Facilities", "Managed Health Care"],
   "Health Care Technology": ["Health Care Technology"]},
  "Pharmaceuticals, Biotechnology & Life Sciences": {
   "Biotechnology": ["Biotechnology"], "Pharmaceuticals": ["Pharmaceuticals"],
   "Life Sciences Tools & Services": ["Life Sciences Tools & Services"]}},
 "Financials": {
  "Banks": {"Banks": ["Diversified Banks", "Regional Banks"]},
  "Financial Services": {
   "Financial Services": ["Diversified Financial Services", "Multi-Sector Holdings", "Specialized Finance",
                          "Commercial & Residential Mortgage Finance", "Transaction & Payment Processing Services"],
   "Consumer Finance": ["Consumer Finance"],
   "Capital Markets": ["Asset Management & Custody Banks", "Investment Banking & Brokerage", "Diversified Capital Markets", "Financial Exchanges & Data"],
   "Mortgage Real Estate Investment Trusts (REITs)": ["Mortgage REITs"]},
  "Insurance": {"Insurance": ["Insurance Brokers", "Life & Health Insurance", "Multi-line Insurance", "Property & Casualty Insurance", "Reinsurance"]}},
 "Information Technology": {
  "Software & Services": {
   "IT Services": ["IT Consulting & Other Services", "Internet Services & Infrastructure"],
   "Software": ["Application Software", "Systems Software"]},
  "Technology Hardware & Equipment": {
   "Communications Equipment": ["Communications Equipment"],
   "Technology Hardware, Storage & Peripherals": ["Technology Hardware, Storage & Peripherals"],
   "Electronic Equipment, Instruments & Components": ["Electronic Equipment & Instruments", "Electronic Components",
                                                       "Electronic Manufacturing Services", "Technology Distributors"]},
  "Semiconductors & Semiconductor Equipment": {
   "Semiconductors & Semiconductor Equipment": ["Semiconductor Materials & Equipment", "Semiconductors"]}},
 "Communication Services": {
  "Telecommunication Services": {
   "Diversified Telecommunication Services": ["Alternative Carriers", "Integrated Telecommunication Services"],
   "Wireless Telecommunication Services": ["Wireless Telecommunication Services"]},
  "Media & Entertainment": {
   "Media": ["Advertising", "Broadcasting", "Cable & Satellite", "Publishing"],
   "Entertainment": ["Movies & Entertainment", "Interactive Home Entertainment"],
   "Interactive Media & Services": ["Interactive Media & Services"]}},
 "Utilities": {"Utilities": {
   "Electric Utilities": ["Electric Utilities"], "Gas Utilities": ["Gas Utilities"], "Multi-Utilities": ["Multi-Utilities"],
   "Water Utilities": ["Water Utilities"],
   "Independent Power and Renewable Electricity Producers": ["Independent Power Producers & Energy Traders", "Renewable Electricity"]}},
 "Real Estate": {
  "Equity Real Estate Investment Trusts (REITs)": {
   "Diversified REITs": ["Diversified REITs"], "Industrial REITs": ["Industrial REITs"], "Hotel & Resort REITs": ["Hotel & Resort REITs"],
   "Office REITs": ["Office REITs"], "Health Care REITs": ["Health Care REITs"],
   "Residential REITs": ["Multi-Family Residential REITs", "Single-Family Residential REITs"], "Retail REITs": ["Retail REITs"],
   "Specialized REITs": ["Other Specialized REITs", "Self-Storage REITs", "Telecom Tower REITs", "Timber REITs", "Data Center REITs"]},
  "Real Estate Management & Development": {
   "Real Estate Management & Development": ["Diversified Real Estate Activities", "Real Estate Operating Companies",
                                            "Real Estate Development", "Real Estate Services"]}},
}

SUB = {}   # sub-industry -> (sector, group, industry)
IND = {}   # industry -> (sector, group)
for sec, groups in TREE.items():
    for grp, inds in groups.items():
        for ind, subs in inds.items():
            IND[ind] = (sec, grp)
            for s in subs:
                SUB[s] = (sec, grp, ind)

SECTOR_CN = {"Information Technology": "信息技术", "Semiconductors": "半导体", "Communication Services": "通信服务",
             "Consumer Discretionary": "可选消费", "Consumer Staples": "日常消费", "Health Care": "医疗保健",
             "Industrials": "工业", "Energy": "能源", "Materials": "原材料", "Utilities": "公用事业",
             "Financials": "金融", "Real Estate": "房地产", "Unclassified": "未分类"}
