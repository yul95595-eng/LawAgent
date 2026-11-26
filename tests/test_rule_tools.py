"""规则引擎工具测试 —— 4 个规则工具的确定性断言。

重点：demo2 场景（工作 3 年 2 个月、月薪 2 万、违法解除）2N = 140000。
"""
from __future__ import annotations

from backend.tools.rule_tools import (
    calculate_claim_amount,
    calculate_limitation_period,
    risk_level_assess,
    validate_contract_format,
)


# ── calculate_limitation_period ─────────────────────────────


class TestLimitation:
    def test_labor_default_one_year(self):
        r = calculate_limitation_period(case_type="labor_termination")
        assert "1 年" in r["period_text"]
        assert "第二十七条" in r["legal_basis"]

    def test_wage_arrears_ongoing_exception(self):
        r = calculate_limitation_period(case_type="labor_termination",
                                       dispute_kind="wage_arrears", relationship_ongoing=True)
        assert "不受 1 年" in r["period_text"]
        assert "第二十七条第四款" in r["legal_basis"]

    def test_civil_three_years(self):
        r = calculate_limitation_period(case_type="contract_sales")
        assert "3 年" in r["period_text"]
        assert "第一百八十八条" in r["legal_basis"]


# ── calculate_claim_amount ──────────────────────────────────


class TestClaimAmount:
    def test_demo2_wrongful_termination_2n(self):
        """demo1 核心口径：3 年 2 个月、月薪 2 万 → 2N = 14 万。"""
        r = calculate_claim_amount("wrongful_termination", 3.17, 20000)
        assert r["formula"] == "2N"
        assert r["months"] == 7.0          # 3.5 个月 × 2 倍
        assert r["amount"] == 140000.0     # 20000 × 3.5 × 2
        assert "第八十七条" in r["legal_basis"]

    def test_economic_compensation_n(self):
        r = calculate_claim_amount("economic_compensation", 3.17, 20000)
        assert r["formula"] == "N"
        assert r["months"] == 3.5
        assert r["amount"] == 70000.0

    def test_no_notice_n_plus_1(self):
        r = calculate_claim_amount("no_notice_termination", 3.17, 20000)
        assert r["formula"] == "N+1"
        assert r["months"] == 4.5
        assert r["amount"] == 90000.0

    def test_under_six_months_half_month(self):
        """不满 6 个月 → 0.5 个月。"""
        r = calculate_claim_amount("economic_compensation", 0.3, 10000)
        assert r["months"] == 0.5
        assert r["amount"] == 5000.0

    def test_salary_cap_twelve_years(self):
        """月薪超社平 3 倍 → 补偿年限最高 12 年（47 条第 2 款）。"""
        r = calculate_claim_amount("economic_compensation", 15, 100000,
                                   salary_over_3x_social_avg=True)
        assert r["months"] == 12
        assert "3 倍" in r["notes"]

    def test_invalid_input(self):
        r = calculate_claim_amount("wrongful_termination", 0, 20000)
        assert r["amount"] == 0


# ── validate_contract_format ────────────────────────────────


_LABOR_OK = """
甲方（用人单位）：某某科技有限公司；乙方（劳动者）：王某。
合同期限：固定期限 3 年，试用期为 2 个月。
工作内容与工作地点：担任工程师，工作地点北京，岗位职责见附件。
工作时间与休息休假：标准工时制，享有法定节假日与年休假。
劳动报酬：月薪 20000 元，每月 10 日发放工资。
社会保险：公司依法为乙方缴纳社会保险。
劳动保护：公司提供符合国家规定的劳动条件与职业危害防护。
"""

_LABOR_BAD = """
甲方：某某公司；乙方：王某。
合同期限：3 年。
工资：面议。
"""


class TestFormatCheck:
    def test_complete_labor_contract(self):
        r = validate_contract_format(_LABOR_OK, contract_kind="labor")
        assert r["complete"] is True
        assert r["missing"] == []
        assert "第十七条" in r["legal_basis"]

    def test_incomplete_labor_contract(self):
        r = validate_contract_format(_LABOR_BAD, contract_kind="labor")
        assert r["complete"] is False
        assert any("社会保险" in m for m in r["missing"])
        assert any("劳动保护" in m for m in r["missing"])

    def test_general_contract(self):
        text = "标的：货物一批；数量与质量标准见清单；价款 10 万元；交付与支付方式；违约责任；争议提交仲裁。"
        r = validate_contract_format(text, contract_kind="general")
        assert r["complete"] is True
        assert "第四百七十条" in r["legal_basis"]


# ── risk_level_assess ───────────────────────────────────────


class TestRiskLevel:
    def test_veto_one_vote_high(self):
        r = risk_level_assess([
            {"risk_type": "trial_over_limit", "risk_name": "试用期超上限", "level": "high"},
        ])
        assert r["risk_level"] == "high"
        assert r["veto_hit"] == "试用期超上限"

    def test_low_only(self):
        r = risk_level_assess([
            {"risk_type": "format_minor", "risk_name": "格式瑕疵", "level": "low"},
            {"risk_type": "format_minor2", "risk_name": "瑕疵2", "level": "low"},
        ])
        assert r["risk_level"] == "low"
        assert r["score"] == 6
        assert r["veto_hit"] == ""

    def test_medium_by_score(self):
        r = risk_level_assess([{"risk_type": "x", "level": "medium"}])
        assert r["risk_level"] == "medium"
        assert r["score"] == 10

    def test_high_by_score_threshold(self):
        r = risk_level_assess([{"risk_type": "x", "level": "medium"},
                               {"risk_type": "y", "level": "high"}])
        assert r["risk_level"] == "high"
        assert r["score"] == 40

    def test_empty(self):
        r = risk_level_assess([])
        assert r["risk_level"] == "low"
        assert r["score"] == 0
