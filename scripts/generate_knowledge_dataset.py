"""生成确定性的虚构银行知识、隔离负例和检索评测集。"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


SEED = 20260919
VERSION = "haiyue-corporate-banking-v2"
BANK = "海岳银行（虚构）"
NOTICE = "仅用于演示与自动化评测，不代表真实银行产品、费率、时效或业务承诺。"


PRODUCTS = [
    ("basic_account", "基本存款账户", "日常经营收付款及现金管理", "营业执照、法定代表人及经办人身份证明、授权文件", "资料齐全后3至5个演示工作日", "开户演示费0元，账户管理演示费每月30元", "一个企业原则上只能开立一个基本存款账户"),
    ("general_account", "一般存款账户", "借款转存、归还和其他结算", "基本户信息、开户证明、授权文件", "资料齐全后2至4个演示工作日", "账户管理演示费每月20元", "演示规则中不办理现金支取"),
    ("temporary_account", "临时存款账户", "临时经营活动和项目资金管理", "项目批文、期限证明、企业证照及授权文件", "资料齐全后3至6个演示工作日", "账户管理演示费每月20元", "有效期届满前须办理展期或销户"),
    ("online_banking", "企业网银", "账户查询、转账、回单和分级授权", "申请表、企业授权书、管理员与操作员信息", "审核通过后1至2个演示工作日", "基础版演示服务费每年120元", "高风险交易仍需复核及风控校验"),
    ("mobile_banking", "企业手机银行", "移动查询、审批和限额内支付", "已开通企业网银、管理员授权及绑定设备", "审核通过后1个演示工作日", "基础功能演示费0元", "不得在越狱或来源不明设备上使用"),
    ("host_to_host", "银企直联", "企业财务系统与银行接口直连", "技术申请、网络方案、授权书和联调联系人", "联调通常需10至20个演示工作日", "一次性演示实施费3000元", "生产开通前必须通过安全与回归测试"),
    ("intrabank_transfer", "行内对公转账", "向本行企业或个人账户付款", "有效账户、付款用途和授权审批", "正常情况下演示为实时处理", "网银渠道演示手续费0元", "异常或高风险交易可能延迟核验"),
    ("interbank_transfer", "跨行对公转账", "向其他银行账户付款", "收款行信息、收款账户、用途和授权审批", "工作时段通常当日或下一演示工作日", "单笔演示手续费5元", "到账时间受清算渠道和收款行影响"),
    ("payroll", "企业代发工资", "批量发放工资和结果回执", "代发协议、员工清单、授权文件和资金安排", "文件校验后按预约日期处理", "每笔演示手续费1元", "不得上传与代发无关的敏感信息"),
    ("cash_pool", "集团资金归集", "多账户余额归集和流动性管理", "集团关系证明、账户授权和归集方案", "方案确认后5至10个演示工作日", "演示服务费每月500元", "跨主体归集必须具备合法授权"),
    ("working_capital_loan", "流动资金贷款", "企业日常生产经营周转", "财务报表、流水、用途材料、征信授权和担保资料", "材料完整后10至20个演示工作日", "利率和相关费用仅以正式审批结果为准", "系统不得承诺额度、利率或审批通过"),
    ("bank_acceptance", "银行承兑汇票", "真实贸易背景下的延期支付", "贸易合同、发票、授信及保证金资料", "材料完整后5至10个演示工作日", "演示承兑费按票面金额0.05%", "必须核验真实贸易背景和授信条件"),
]


def record(product: tuple[str, ...], domain: str, topic: str, index: int) -> dict:
    code, name, purpose, materials, duration, fee, restriction = product
    contents = {
        "product": {
            "overview": f"{name}用于{purpose}。适用对象为已完成身份核验且业务需求明确的企业客户。",
            "audience": f"{name}主要面向需要{purpose}的企业；办理前需确认主体资格、授权关系和实际用途。",
            "features": f"{name}的演示能力围绕{purpose}，可在授权范围内查询进度和获取业务回执。",
            "prerequisites": f"使用{name}前需提交{materials}，并完成客户尽职调查及必要的风险核验。",
            "limitations": f"{name}存在以下业务边界：{restriction}。客服只能提供咨询，不能代替正式审核或执行交易。",
        },
        "process": {
            "application": f"申请{name}时，先预约并确认需求，再提交{materials}，随后由银行进行完整性和风险审核。",
            "materials": f"{name}常见演示材料包括：{materials}。材料清单会随主体类型和业务场景变化。",
            "steps": f"{name}演示流程为需求确认、资料提交、身份与授权核验、风险审核、结果通知和后续启用。",
            "duration": f"{name}在材料齐全且无补充核验时，参考处理时间为{duration}；该时效仅为虚构演示参数。",
            "change_cancel": f"变更或停用{name}需由有权人员提交申请和授权材料；未完成核验前原配置不自动失效。",
        },
        "policy": {
            "fees": f"{name}的合成收费规则为：{fee}。实际演示回答必须同时说明这是虚构参数。",
            "effective": f"{name}当前知识版本自2026-01-01起在演示环境生效，后续版本不得覆盖历史记录。",
            "approval": f"{name}是否可办理取决于身份、授权、资料和风险审核；智能客服不得承诺审批结果。",
            "security": f"办理{name}不得在对话中提交密码、验证码或安全介质口令；敏感材料应进入受控渠道。",
            "exceptions": f"当{name}出现资料冲突、授权不清或高风险信号时暂停办理并转人工核验；边界为：{restriction}。",
        },
    }
    query_templates = {
        "overview": [f"{name}主要是做什么的？", f"介绍一下{name}的用途"],
        "audience": [f"什么企业适合办理{name}？", f"哪些公司可以用{name}"],
        "features": [f"{name}能提供哪些功能？", f"用{name}可以做什么"],
        "prerequisites": [f"使用{name}前要满足什么条件？", f"办理{name}有哪些前提"],
        "limitations": [f"{name}有哪些限制？", f"{name}不能做什么"],
        "application": [f"怎样申请{name}？", f"{name}从哪里开始办理"],
        "materials": [f"办理{name}需要什么资料？", f"申请{name}要准备哪些材料"],
        "steps": [f"{name}办理步骤是什么？", f"说一下{name}完整流程"],
        "duration": [f"{name}办理要多久？", f"申请{name}大概多长时间"],
        "change_cancel": [f"怎样变更或停用{name}？", f"{name}不想用了怎么取消"],
        "fees": [f"{name}怎么收费？", f"使用{name}需要多少钱"],
        "effective": [f"{name}这版规则什么时候生效？", f"当前{name}知识版本的生效日期是什么"],
        "approval": [f"{name}能保证办下来吗？", f"申请{name}一定会通过吗"],
        "security": [f"办理{name}可以把验证码发给客服吗？", f"{name}咨询时哪些敏感信息不能发"],
        "exceptions": [f"{name}遇到高风险情况怎么办？", f"什么情况下{name}要转人工核验"],
    }
    return {
        "id": f"{domain}-{code}-{topic}",
        "title": f"{name}-{topic}",
        "domain": domain,
        "product_code": code,
        "product": name,
        "topic": topic,
        "content": contents[domain][topic],
        "keywords": [name, purpose, topic],
        "source": f"{BANK}对公业务演示手册",
        "effective_at": "2026-01-01T00:00:00+08:00",
        "expires_at": None,
        "version": "2.0",
        "status": "published",
        "audience": "demo",
        "synthetic": True,
        "notice": NOTICE,
        "sequence": index,
        "queries": query_templates[topic],
    }


def generate_rows() -> tuple[list[dict], list[dict], list[dict]]:
    topics = {
        "product": ["overview", "audience", "features", "prerequisites", "limitations"],
        "process": ["application", "materials", "steps", "duration", "change_cancel"],
        "policy": ["fees", "effective", "approval", "security", "exceptions"],
    }
    knowledge = []
    retrieval = []
    sequence = 0
    for product_index, product in enumerate(PRODUCTS):
        split = "dev" if product_index < 3 else "validation" if product_index < 6 else "test"
        for domain, names in topics.items():
            for topic in names:
                sequence += 1
                item = record(product, domain, topic, sequence)
                questions = item.pop("queries")
                knowledge.append(item)
                for variant, query in enumerate(questions, 1):
                    retrieval.append({
                        "id": f"q-{item['id']}-{variant}",
                        "query": query,
                        "expected_ids": [item["id"]],
                        "domain": domain,
                        "product_code": product[0],
                        "split": split,
                        "case_type": "direct" if variant == 1 else "paraphrase",
                        "synthetic": True,
                    })
    controls = []
    control_types = [
        ("unpublished", "draft", "2026-01-01T00:00:00+08:00", None),
        ("inactive", "inactive", "2026-01-01T00:00:00+08:00", None),
        ("expired", "published", "2025-01-01T00:00:00+08:00", "2025-12-31T23:59:59+08:00"),
        ("future", "published", "2099-01-01T00:00:00+08:00", None),
        ("restricted", "published", "2026-01-01T00:00:00+08:00", None),
    ]
    for product in PRODUCTS:
        for kind, status, effective, expires in control_types:
            controls.append({
                "id": f"control-{product[0]}-{kind}",
                "title": f"{product[1]}-{kind}-过滤样本",
                "domain": "policy",
                "product_code": product[0],
                "product": product[1],
                "content": f"这是{kind}过滤测试记录，不得作为正常客服证据返回。",
                "source": f"{BANK}隔离测试数据",
                "effective_at": effective,
                "expires_at": expires,
                "version": "control-1.0",
                "status": status,
                "audience": "restricted" if kind == "restricted" else "demo",
                "synthetic": True,
                "control_type": kind,
                "must_not_retrieve": True,
            })
    return knowledge, controls, retrieval


def write_jsonl(path: Path, rows: list[dict]) -> dict:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")
    return {"rows": len(rows), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def generate(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    knowledge, controls, retrieval = generate_rows()
    files = {
        "knowledge.jsonl": write_jsonl(output / "knowledge.jsonl", knowledge),
        "knowledge_controls.jsonl": write_jsonl(output / "knowledge_controls.jsonl", controls),
        "retrieval_eval.jsonl": write_jsonl(output / "retrieval_eval.jsonl", retrieval),
    }
    manifest = {
        "data_version": VERSION,
        "generated_at": "2026-09-19T00:00:00+08:00",
        "seed": SEED,
        "bank": BANK,
        "synthetic": True,
        "notice": NOTICE,
        "counts": {
            "knowledge_by_domain": dict(sorted(Counter(row["domain"] for row in knowledge).items())),
            "controls": len(controls),
            "retrieval_by_split": dict(sorted(Counter(row["split"] for row in retrieval).items())),
        },
        "files": files,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "datasets" / "generated"))
    args = parser.parse_args()
    print(json.dumps(generate(Path(args.output)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
