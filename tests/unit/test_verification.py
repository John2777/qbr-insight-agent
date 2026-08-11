from __future__ import annotations

from packages.qbr_core.analysis.verification import ClaimEvidenceVerifier, markdown_format_integrity, numeric_facts


def _evidence(quote: str) -> list[dict[str, object]]:
    return [{"document_title": "QBR", "slide_no": 3, "quote": quote, "content_role": "business_fact"}]


def test_numeric_facts_parse_numbers_next_to_chinese_and_ignore_markdown_ordinals() -> None:
    facts = numeric_facts(
        "1. 股东资本比率从240.2%降至221%。[1]\n**2. 回购需要关注。**[2]\n### **3. 数据局限**"
    )

    assert [(fact.value, fact.unit) for fact in facts] == [("240.2", "percent"), ("221", "percent")]
    assert numeric_facts("20 management actions")[0].value == "20"


def test_verifier_accepts_contextual_percent_unit_when_source_chart_omits_symbol() -> None:
    quote = "股东资本比率 | primary | 0 | 24/10=240.2; 当前=221"
    result = ClaimEvidenceVerifier().verify(
        "股东资本比率从240.2%降至221%。[1]",
        fallback=quote,
        evidence=_evidence(quote),
    )

    assert result.accepted
    assert result.warnings == ()
    assert result.diagnostics["disposition"] == "accepted"


def test_verifier_normalizes_currency_scales_but_rejects_conflicting_explicit_units() -> None:
    verifier = ClaimEvidenceVerifier()
    quote = "Net FSG: US$4.451bn"

    equivalent = verifier.verify("Net FSG 为 USD 4,451m。[1]", fallback=quote, evidence=_evidence(quote))
    conflict = verifier.verify("Net FSG 为4451000000%。[1]", fallback=quote, evidence=_evidence(quote))

    assert equivalent.accepted
    assert not conflict.accepted
    assert conflict.warnings == ("LLM_NUMERIC_VALIDATION_FAILED",)


def test_verifier_removes_only_the_unsupported_claim_segment() -> None:
    quote = "股东资本比率当前为221%，绿色阈值为210%"
    answer = "资本比率目前为221%，仍高于文档绿色阈值。[1]\n安全缓冲为12个百分点。[1]"

    result = ClaimEvidenceVerifier().verify(answer, fallback=quote, evidence=_evidence(quote))

    assert result.accepted
    assert result.repaired_answer == "资本比率目前为221%，仍高于文档绿色阈值。[1]"
    assert result.warnings == ("LLM_NUMERIC_VALIDATION_FAILED",)
    assert result.diagnostics["disposition"] == "repaired"
    assert result.diagnostics["removed_claim_segments"] == 1


def test_verifier_accepts_simple_auditable_derived_values() -> None:
    quote = "资本比率当前为226%，管理层预警线为190%"
    answer = "当前高于预警线36个百分点，相当于预警线的18.9%，缓冲较充足。[1]"

    result = ClaimEvidenceVerifier().verify(answer, fallback=quote, evidence=_evidence(quote))

    assert result.accepted
    assert result.warnings == ()
    assert result.diagnostics["disposition"] == "accepted"


def test_verifier_propagates_table_currency_units_to_numeric_cells() -> None:
    quote = "指标 | 2024A | 2025A | 单位\nVONB | 4,712 | 5,516 | US$m"
    answer = "VONB从US$4,712m增长至US$5,516m，增加US$804m。[1]"

    result = ClaimEvidenceVerifier().verify(answer, fallback=quote, evidence=_evidence(quote))

    assert result.accepted
    assert result.warnings == ()


def test_verifier_tracks_mixed_table_units_by_column_and_accepts_display_values() -> None:
    quote = (
        "渠道 | 线索k | 签发k | APE US$m | CAC US$ | 继续率\n"
        "数字直销 | 4,800 | 575 | 920 | 74 | 84.1%"
    )
    answer = "数字直销有4,800k线索、575k签发，APE为US$920m，CAC为US$74，继续率84.1%。[1]"

    result = ClaimEvidenceVerifier().verify(answer, fallback=quote, evidence=_evidence(quote))

    assert result.accepted
    assert result.warnings == ()


def test_repair_preserves_bold_numbered_heading_structure() -> None:
    quote = "资本比率当前为221%"
    answer = "虚构值为999%。[1]\n\n**3. 其他相关指标**\n资本比率为221%。[1]"

    result = ClaimEvidenceVerifier().verify(answer, fallback=quote, evidence=_evidence(quote))

    assert result.accepted
    assert result.repaired_answer == "**3. 其他相关指标**\n资本比率为221%。[1]"
    assert result.repaired_answer.count("**") % 2 == 0


def test_markdown_integrity_accepts_thematic_breaks_but_rejects_unclosed_emphasis() -> None:
    assert markdown_format_integrity("**结论**\n\n***\n\n依据")
    assert markdown_format_integrity("__结论__\n\n_ _ _\n\n依据")
    assert not markdown_format_integrity("**未闭合")


def test_verifier_falls_back_when_no_grounded_claim_survives() -> None:
    quote = "股东资本比率当前为221%"
    result = ClaimEvidenceVerifier().verify("安全缓冲为11个百分点。[1]", fallback=quote, evidence=_evidence(quote))

    assert not result.accepted
    assert result.repaired_answer is None
    assert result.diagnostics["disposition"] == "fallback"


def test_verifier_repairs_english_sentences_without_splitting_decimal_values() -> None:
    quote = "Revenue was 20.5"
    answer = "Revenue was 20.5. [1] Unsupported increase was 5.2%. [1]"

    result = ClaimEvidenceVerifier().verify(answer, fallback=quote, evidence=_evidence(quote))

    assert result.accepted
    assert result.repaired_answer == "Revenue was 20.5. [1]"


def _chart_bundle_evidence() -> list[dict[str, object]]:
    scope = {
        "kind": "chart_analysis",
        "selected_series_names": [
            "香港",
            "中国内地",
            "泰国",
            "新加坡",
            "其他市场",
            "VONB Margin",
            "13M Persistency",
            "Digital STP",
            "Agent Productivity",
            "Protection Mix",
        ],
        "family_series": {
            "bar": ["香港", "中国内地", "泰国", "新加坡", "其他市场"],
            "line": ["VONB Margin", "13M Persistency", "Digital STP", "Agent Productivity", "Protection Mix"],
        },
        "excluded_document_series_names": ["OPAT", "UFSG", "Market Risk", "Credit Risk"],
        "minimum_family_mentions": 2,
        "no_pairwise_mapping": True,
    }
    return [
        {
            "document_title": "QBR",
            "slide_no": 4,
            "quote": "香港=238; 中国内地=249; VONB Margin=59.7; Protection Mix=46.3",
            "content_role": "chart",
            "chart_scope": scope,
        }
    ]


def test_verifier_rejects_cross_slide_series_substitution_in_chart_analysis() -> None:
    evidence = _chart_bundle_evidence()
    answer = "香港与中国内地产出增长，但OPAT与UFSG可能代表两项质量指标。[1]"

    result = ClaimEvidenceVerifier().verify(answer, fallback=str(evidence[0]["quote"]), evidence=evidence)

    assert not result.accepted
    assert "LLM_CHART_SCOPE_VALIDATION_FAILED" in result.warnings
    assert result.diagnostics["chart_scope"]["outside_scope_series"] == ["OPAT", "UFSG"]


def test_verifier_requires_business_series_coverage_for_exhaustive_chart_analysis() -> None:
    evidence = _chart_bundle_evidence()
    answer = "香港产出提高，同时VONB Margin改善。[1]"

    result = ClaimEvidenceVerifier().verify(answer, fallback=str(evidence[0]["quote"]), evidence=evidence)

    assert not result.accepted
    assert "LLM_CHART_COVERAGE_VALIDATION_FAILED" in result.warnings
    assert set(result.diagnostics["chart_scope"]["missing_family_coverage"]) == {"bar", "line"}


def test_verifier_requires_source_series_names_without_a_maintained_alias_dictionary() -> None:
    evidence = _chart_bundle_evidence()
    answer = "香港和中国内地贡献主要产出；新业务价值率改善，但保障业务占比回落。[1]"

    result = ClaimEvidenceVerifier().verify(answer, fallback=str(evidence[0]["quote"]), evidence=evidence)

    assert not result.accepted
    assert "LLM_CHART_COVERAGE_VALIDATION_FAILED" in result.warnings


def test_verifier_accepts_normalized_source_series_names() -> None:
    evidence = _chart_bundle_evidence()
    answer = "香港和中国内地贡献主要产出；VONB-Margin改善，但Protection Mix回落。[1]"

    result = ClaimEvidenceVerifier().verify(answer, fallback=str(evidence[0]["quote"]), evidence=evidence)

    assert result.accepted
    assert result.warnings == ()
