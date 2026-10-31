"""
GapHunter 词库 v0.3(从 gap_hunter 拆出,供其与 hypothesis 共用)
- ENTITY_SYNONYMS:实体标签 -> 英文检索同义词
- RESOLUTION_REL / RESOLUTION_ENDPOINTS:反查"解决型"检索词块
- TOPIC_OUTCOME_SYNONYMS:结局概念 -> 同义词(跨主题判定用)
"""

ENTITY_SYNONYMS: dict = {
    "HIF-1": ["HIF-1", "HIF1A", "hypoxia-inducible factor 1"],
    "HIF-2": ["HIF-2", "EPAS1", "hypoxia-inducible factor 2"],
    "VEGF": ["VEGF", "vascular endothelial growth factor", "angiogenesis"],
    "adenosine axis": ["adenosine", "CD73", "NT5E", "CD39", "A2A receptor"],
    "lactate": ["lactate", "lactic acid", "acidosis"],
    "TAM": ["tumor-associated macrophage", "TAM"],
    "MDSC": ["myeloid-derived suppressor cell", "MDSC"],
    "T cell exhaustion": ["T cell exhaustion", "exhausted T cell"],
    "TIL/CD8 infiltration": ["CD8", "tumor-infiltrating lymphocyte", "TIL"],
    "CXCR4 axis": ["CXCR4", "CXCL12"],
    "ECM remodeling": ["extracellular matrix", "ECM", "cancer-associated fibroblast"],
    "ROS": ["reactive oxygen species", "ROS", "oxidative stress"],
    "autophagy": ["autophagy"],
    "metabolic reprogramming": ["metabolic reprogramming", "glycolysis"],
    "epigenetic": ["epigenetic", "histone modification", "DNA methylation"],
    "exosome": ["exosome", "extracellular vesicle", "exosomal"],
    "macrophage polarization": ["macrophage polarization", "M2 macrophage", "M1 macrophage"],
}

RESOLUTION_REL = ['"mediates"', '"is necessary for"', '"is required for"',
                  '"causal role"', '"directly regulates"']
RESOLUTION_ENDPOINTS = ["response", "resistance", "efficacy", "survival",
                        "metastasis", "polarization"]

# 主题结局概念同义词(判定"该文结局是否属于当前主题结局")
TOPIC_OUTCOME_SYNONYMS: dict = {
    "resistance": ["resistance", "resistant", "non-response", "treatment failure",
                   "immune evasion"],
    "metastasis": ["metastasis", "metastatic", "metastases", "invasion",
                   "migration", "dissemination"],
    "response": ["response", "efficacy", "tumor control"],
    "polarization": ["polarization", "M2 macrophage", "M1 macrophage"],
}

DESIGN_ZH = {"clinical": "临床研究", "human": "人体样本研究", "in_vivo": "动物/体内研究",
             "in_vitro": "体外/细胞研究", "bioinformatic": "生物信息分析", "review": "综述",
             "unknown": "设计未识别"}
