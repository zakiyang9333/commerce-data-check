"""Radar sub-segments the spec asks for (GPU, CPU, memory, foundry ... SaaS,
cyber-security ...). GICS has no such levels, so these are a hand-kept list
based on each company's own 10-K/20-F business description (main revenue
source). Source label: 人工整理（依据公司年报业务描述）; confidence medium;
status 待核实 until checked against the latest annual report. A company not
listed keeps only its GICS/SIC levels (e.g. "半导体（未细分）")."""

SEG = {
 # --- semiconductors
 "GPU": ["NVDA"],
 "CPU/GPU": ["AMD"],
 "CPU": ["INTC", "ARM"],
 "存储芯片": ["MU", "SNDK"],
 "存储设备（硬盘）": ["WDC", "STX"],
 "晶圆代工": ["TSM", "GFS", "UMC", "TSEM"],
 "封装测试": ["AMKR", "ASX"],
 "半导体设备": ["AMAT", "LRCX", "KLAC", "ASML", "TER", "ONTO", "CAMT", "NVMI", "ACMR", "ICHR", "UCTT", "KLIC", "COHU",
              "FORM", "VECO", "MKSI", "AEIS", "ASYS", "AMBQ"],
 "半导体材料": ["ENTG", "PLAB", "AXTI"],
 "芯片设计": ["AVGO", "QCOM", "MRVL", "MPWR", "LSCC", "SLAB", "RMBS", "CRUS", "SITM", "SMTC", "MTSI", "AMBA", "SYNA",
            "ALAB", "CRDO", "SWKS", "QRVO", "HIMX", "MXL", "INDI", "NVTS", "POWI", "PI", "NVEC", "CEVA", "QUIK", "GSIT"],
 "模拟与功率芯片（IDM）": ["TXN", "ADI", "ON", "NXPI", "MCHP", "STM", "VSH", "DIOD", "AOSL", "ALGM", "WOLF"],
 # --- software & AI
 "SaaS": ["CRM", "NOW", "WDAY", "HUBS", "TEAM", "VEEV", "PAYC", "PCTY", "DOCU", "ZM", "BL", "APPF", "MNDY", "WK", "BOX",
          "DBX", "SPSC", "PAYX", "TOST", "BILL", "FRSH", "ASAN", "CWAN", "INTA", "QTWO", "ALKT", "DOCS", "PCOR"],
 "企业软件": ["ORCL", "SAP", "ADBE", "INTU", "ADSK", "PTC", "MANH", "TYL", "GWRE", "PEGA", "BSY", "FICO", "SSNC", "PLTR",
            "ROP", "DT", "ESTC", "CDNS", "SNPS", "APP"],
 "云计算": ["MSFT", "NET", "DOCN", "SNOW", "MDB", "AKAM", "FSLY", "DDOG"],
 "AI基础设施": ["ANET", "SMCI", "VRT", "CRWV", "NBIS", "CLS", "COHR", "LITE", "DELL", "HPE"],
 "网络安全": ["PANW", "CRWD", "FTNT", "ZS", "OKTA", "S", "CYBR", "CHKP", "QLYS", "TENB", "RPD", "VRNS", "GEN", "SAIL", "RBRK"],
 "数据中心": ["EQIX", "DLR", "APLD", "IREN"],
}
TICKER_SEG = {t: seg for seg, ts in SEG.items() for t in ts}
SEMI_SEGMENTS = {"GPU", "CPU/GPU", "CPU", "存储芯片", "晶圆代工", "封装测试", "半导体设备", "半导体材料", "芯片设计", "模拟与功率芯片（IDM）"}
SOFTWARE_SEGMENTS = {"SaaS", "企业软件", "云计算", "AI基础设施", "网络安全", "数据中心"}
SOURCE = "人工整理（依据公司年报业务描述，待核实）"
