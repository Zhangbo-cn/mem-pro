#!/usr/bin/env python3
"""生成可控的 α/β 调优集（确定性，无随机数）。

动机：手写的 tools/eval_queries.json 只有 49 条 query，拆一半后 α/β 结论互相矛盾
（每条 query 值 2 个点，噪声盖过信号）。这里用模板批量生成，保证：
  - 每个 query 有唯一 gold（属性查找）
  - 同人不同属性的高词面重叠干扰项
  - 同属性不同人的干扰项
  - 时间线干扰（旧住址 / 旧工作）
  - 中英混排 + 跨语言 query
  - 纯改写（"不耐受" vs "过敏"）无词面重叠

输出：tools/eval_gen.json（可重复生成，字节稳定）
"""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 20 个人：一半中文名 / 一半英文名，用来测跨语言
ZH_NAMES = ["张伟", "李娜", "王芳", "刘洋", "陈静", "赵磊", "孙悦", "周涛", "吴敏", "郑凯"]
EN_NAMES = ["Alice", "Bob", "Carol", "David", "Emma", "Frank", "Grace", "Henry", "Ivy", "Jack"]

CITIES = ["柏林", "慕尼黑", "里斯本", "上海", "成都", "杭州", "武汉", "西安", "南京", "苏州"]
OLD_CITIES = ["北京", "广州", "天津", "重庆", "青岛", "长沙", "合肥", "福州", "昆明", "大连"]
JOBS = ["牙医", "后端工程师", "产品经理", "会计师", "中学老师", "兽医", "建筑师", "律师", "数据分析师", "摄影师"]
COMPANIES = ["西门子", "字节跳动", "博世", "德勤", "SAP", "阿里巴巴", "大众", "安联", "飞利浦", "蔡司"]
FOODS = ["花生", "海鲜", "牛奶", "鸡蛋", "芒果", "坚果", "小麦", "大豆", "芝麻", "菠萝"]
PETS = ["Momo", "Lucky", "豆豆", "Coco", "咪咪", "Bella", "阿黄", "Max", "团子", "Rocky"]
ANIMALS = ["橘猫", "柴犬", "兔子", "鹦鹉", "仓鼠", "布偶猫", "柯基", "乌龟", "雪貂", "边牧"]
MONTHS = [3, 5, 7, 9, 11, 1, 4, 6, 8, 12]

DOC_TMPL = {
    "city": ["{n} 现在住在{c}。", "{n} currently lives in {c}."],
    "job": ["{n} 在{co}做{j}。", "{n} works as a {j} at {co}."],
    "allergy": ["{n} 对{f}过敏，吃饭要避开。", "{n} is allergic to {f}."],
    "pet": ["{n} 养了一只叫{p}的{a}。", "{n} has a {a} named {p}."],
}

# 每个属性三种问法：直问 / 改写（少或无词面重叠） / 反问
QUERY_TMPL = {
    "city": ["{n} 现在住在哪个城市？", "{n} 目前落脚在哪里？", "Where does {n} live now?"],
    "job": ["{n} 做什么工作？", "{n} 的职业是什么？", "What does {n} do for a living?"],
    "allergy": ["{n} 对什么食物过敏？", "{n} 吃什么东西会不舒服？", "What is {n} allergic to?"],
    "pet": ["{n} 的宠物叫什么？", "{n} 家里养了什么小动物？", "What pet does {n} have?"],
}

OLD_DOC = {
    "city": ["{n} 以前住在{oc}，后来搬走了。", "{n} used to live in {oc}."],
    "job": ["{n} 上一份工作在{co}，已经离职。", "{n} previously worked at {co}."],
}


def main() -> int:
    docs: list[dict] = []
    queries: list[dict] = []
    kinds_count: dict[str, int] = {}
    idx = 0

    def add_doc(text: str, tag: str) -> str:
        nonlocal idx
        idx += 1
        did = f"g{idx:03d}"
        docs.append({"id": did, "text": text, "tag": tag})
        return did

    for pi, (zn, en) in enumerate(zip(ZH_NAMES, EN_NAMES)):
        bi = pi % 2  # 轮换语言，制造跨语言检索
        city, ocity = CITIES[pi], OLD_CITIES[pi]
        job, comp, food = JOBS[pi], COMPANIES[pi], FOODS[pi]
        pet, animal = PETS[pi], ANIMALS[pi]
        names = (zn, en)
        name_of: dict[str, str] = {}

        for ai, attr in enumerate(("city", "job", "allergy", "pet")):
            n = names[(bi + ai) % 2]  # 同一人的不同属性用不同语言
            name_of[attr] = n
            if attr == "city":
                did = add_doc(DOC_TMPL[attr][bi].format(n=n, c=city), f"{attr}:{n}")
            elif attr == "job":
                did = add_doc(DOC_TMPL[attr][bi].format(n=n, j=job, co=comp), f"{attr}:{n}")
            elif attr == "allergy":
                did = add_doc(DOC_TMPL[attr][bi].format(n=n, f=food), f"{attr}:{n}")
            else:
                did = add_doc(DOC_TMPL[attr][bi].format(n=n, p=pet, a=animal), f"{attr}:{n}")

            # 每条属性配 2 个 query（直问 + 改写/跨语言）
            for qi in (0, 1 + (pi % 2)):
                qt = QUERY_TMPL[attr][(qi + pi) % 3].format(n=n)
                kind = ["direct", "paraphrase", "cross-lingual"][(qi + pi) % 3]
                kinds_count[kind] = kinds_count.get(kind, 0) + 1
                queries.append({"q": qt, "gold": [did], "kind": kind})

        # 时间线干扰：旧住址 / 旧工作，问"现在"
        if pi % 2 == 0:
            who = name_of["city"]          # 用「真的有 city 文档」的那个人，否则 gold 会落空
            add_doc(OLD_DOC["city"][pi % 2].format(n=who, oc=ocity), f"old-city:{who}")
            queries.append({"q": QUERY_TMPL["city"][0].format(n=who),
                            "gold": [d["id"] for d in docs if d["tag"] == f"city:{who}"],
                            "kind": "temporal"})
            kinds_count["temporal"] = kinds_count.get("temporal", 0) + 1
        else:
            who = name_of["job"]
            add_doc(OLD_DOC["job"][pi % 2].format(n=who, co=COMPANIES[(pi + 3) % 10]),
                    f"old-job:{who}")
            queries.append({"q": QUERY_TMPL["job"][0].format(n=who),
                            "gold": [d["id"] for d in docs if d["tag"] == f"job:{who}"],
                            "kind": "temporal"})
            kinds_count["temporal"] = kinds_count.get("temporal", 0) + 1

        # 生日：纯数字 needle
        month = MONTHS[pi]
        did = add_doc(f"{names[1]} 的生日是 {month} 月 {pi + 1} 日。", f"birthday:{names[1]}")
        queries.append({"q": f"{names[1]} 的生日是哪天？", "gold": [did], "kind": "needle"})
        kinds_count["needle"] = kinds_count.get("needle", 0) + 1

    out = {
        "_note": "由 tools/gen_eval.py 确定性生成，勿手改。用于 α/β 调优的统计功率补充。",
        "docs": docs,
        "queries": queries,
    }
    path = os.path.join(ROOT, "tools", "eval_gen.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print(f"生成 {path}: {len(docs)} docs / {len(queries)} queries")
    print("query 类型分布:", json.dumps(kinds_count, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
