"""Stage synthetic linked customers and multi-intent cases; never mutate ABCD/Git."""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = PROJECT_ROOT / "raw_data"
OUT = PROJECT_ROOT / "processed_data" / "customer_simulation"
OUT.mkdir(parents=True, exist_ok=True)
SEED = 1252026
RNG = random.Random(SEED)
INTENTS = ("return_size", "return_color", "return_stain", "promo_code_invalid",
           "promo_code_out_of_date", "manage_cancel", "manage_upgrade", "manage_downgrade")
FAMILY = {x: "return" for x in INTENTS[:3]} | {x: "promo" for x in INTENTS[3:5]} | {x: "manage" for x in INTENTS[5:]}
SPLITS = ("train", "dev", "test")
TRIPLE_QUOTA = {"train": 64, "dev": 8, "test": 8}
DOUBLE_QUOTA = {"train": 145, "dev": 17, "test": 18}
FIRST = "Aaliyah Aaron Abigail Adam Adrian Aiden Aisha Alan Alexa Alice Alicia Amara Amelia Amir Ana Andre Angela Anita Aria Asher Audrey Ava Benjamin Bianca Blake Brandon Brianna Caleb Camila Carlos Carmen Charlotte Chloe Clara Daniel Daria David Diana Diego Elena Eli Elias Elijah Emily Emma Eric Ethan Eva Fatima Felix Fiona Gabriel Grace Hannah Harper Henry Isaac Isabella Jack Jackson Jacob Jade James Jasmine Javier Jayden Jennifer Jessica Jordan Jose Joshua Julia Kai Katherine Kevin Leah Leo Liam Lily Logan Lucas Lucy Luna Madison Marcus Maria Maya Mia Michael Naomi Nathan Noah Nora Olivia Omar Oscar Owen Priya Rachel Rafael Rebecca Riley Rosa Ryan Samira Sara Sebastian Sofia Sophia Stella Taylor Theo Thomas Victor Victoria William Yara Zoe".split()
LAST = "Adams Ahmed Allen Alvarez Anderson Baker Bennett Brooks Brown Carter Chen Clarke Collins Cooper Davis Diaz Edwards Evans Flores Foster Garcia Gomez Gray Green Hall Harris Hayes Henderson Hill Hughes Jackson James Johnson Khan Kim Lee Lewis Lopez Martin Martinez Miller Mitchell Moore Morgan Murphy Nelson Nguyen Ortiz Parker Patel Perez Phillips Price Ramirez Reed Reyes Rivera Robinson Rogers Ross Russell Sanchez Scott Sharma Singh Smith Stewart Sullivan Tan Taylor Thomas Thompson Torres Turner Walker Ward Watson White Williams Wright Young Zhao".split()

def write_jsonl(path: Path, rows):
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

def make_groups(rows):
    pools = defaultdict(list)
    for split, convo in rows:
        key = (split, convo["scenario"]["personal"]["member_level"], FAMILY[convo["scenario"]["subflow"]])
        pools[key].append((split, convo))
    for pool in pools.values(): RNG.shuffle(pool)
    groups = []
    for split in SPLITS:
        for _ in range(TRIPLE_QUOTA[split]):
            choices = [(min(len(pools[(split,t,f)]) for f in ("return","promo","manage")),t)
                       for t in ("bronze","silver","gold","guest")]
            _, tier = max(choices)
            if not all(pools[(split,tier,f)] for f in ("return","promo","manage")):
                raise ValueError("triple quota infeasible")
            groups.append([pools[(split,tier,f)].pop() for f in ("return","promo","manage")])
        for _ in range(DOUBLE_QUOTA[split]):
            choices = []
            for tier in ("bronze","silver","gold","guest"):
                for f1,f2 in (("return","promo"),("return","manage"),("promo","manage")):
                    a,b = pools[(split,tier,f1)],pools[(split,tier,f2)]
                    if a and b: choices.append((min(len(a),len(b)),len(a)+len(b),tier,f1,f2))
            if not choices: raise ValueError("double quota infeasible")
            _,_,tier,f1,f2 = max(choices)
            groups.append([pools[(split,tier,f1)].pop(),pools[(split,tier,f2)].pop()])
    for pool in pools.values():
        groups.extend([(split,c)] for split,c in pool)
    RNG.shuffle(groups)
    assert sum(map(len,groups)) == len(rows)
    return groups

def name_pool():
    names=[(first,last) for first in FIRST for last in LAST]
    RNG.shuffle(names)
    return names

def replace_exact(text, old, new):
    if not old or old == new: return text
    pattern=re.compile(r"(?<![A-Za-z0-9])"+re.escape(str(old))+r"(?![A-Za-z0-9])",re.I)
    return pattern.sub(lambda _: str(new),text)

def rewrite_text(text, old, new):
    # Longest first keeps email from being partially consumed by username replacement.
    fields=("email","phone","customer_name","username","account_id")
    replacements=sorted(((str(old.get(k)),str(new[k])) for k in fields if old.get(k)),key=lambda z:-len(z[0]))
    for source,target in replacements: text=replace_exact(text,source,target)
    old_parts=str(old.get("customer_name","")).split()
    new_parts=new["customer_name"].split()
    if len(old_parts)==len(new_parts)==2:
        protected=[]
        def hold(match):
            protected.append(match.group(0))
            return f"¤{len(protected)-1}¤"
        text=re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|https?://\S+",hold,text)
        for source,target in zip(old_parts,new_parts):text=replace_exact(text,source,target)
        for i,value in enumerate(protected):text=text.replace(f"¤{i}¤",value)
    return text

def request_phrase(intent, order_id, product_name):
    product_name=product_name.replace("_"," ")
    if intent == "return_size": return f"I need to return the {product_name} in order {order_id}; the size is wrong."
    if intent == "return_color": return f"I need to return the {product_name} in order {order_id}; I dislike the color."
    if intent == "return_stain": return f"There is a stain on the {product_name} in order {order_id}, and I want to return it."
    if intent == "promo_code_invalid": return "A promo code I received five days ago does not work."
    if intent == "promo_code_out_of_date": return "A promo code I received two weeks ago does not work."
    if intent == "manage_cancel": return f"I accidentally ordered an extra item in order {order_id}; please help me cancel it."
    if intent == "manage_upgrade": return f"Please upgrade shipping for order {order_id} to overnight."
    if intent == "manage_downgrade": return f"Please change shipping for order {order_id} to next week."
    raise ValueError(intent)

def build():
    with gzip.open(ROOT / "abcd_v1.1.json.gz","rt",encoding="utf-8") as f: data=json.load(f)
    rows=[(split,c) for split in SPLITS for c in data[split]
          if c["scenario"]["subflow"] in INTENTS]
    groups=make_groups(rows)
    names=name_pool()
    if len(groups)>len(names): raise ValueError("insufficient unique names")
    customers=[];orders=[];items=[];cases=[];conversations=[];group_rows=[]
    source_identity_leak=[]
    identity_review_flags=[]
    for i,group in enumerate(groups,1):
        first,last=names[i-1]
        split=group[0][0];tier=group[0][1]["scenario"]["personal"]["member_level"]
        assert all(s==split and c["scenario"]["personal"]["member_level"]==tier for s,c in group)
        cid=f"C{i:04d}"; username=f"{first.lower()}.{last.lower()}.{i:04d}"
        person={"customer_id":cid,"customer_name":f"{first} {last}","account_id":f"ACCT-{i:06d}",
                "username":username,"email":f"{username}@example.test","phone":f"+1-000-555-{i:04d}",
                "member_level":tier,"split":split,"identity_source":"synthetic"}
        customers.append(person)
        refs=[]
        for s,c in group:
            scenario=c["scenario"];old=scenario["personal"]
            intent=scenario["subflow"];order=scenario["order"]
            scenario_personal={k:person[k] for k in ("customer_name","account_id","username","email","phone","member_level")}
            order_id=order["order_id"]
            status=order.get("shipping_status")
            order_row={"order_id":order_id,"customer_id":cid,"source_convo_id":c["convo_id"],
                       "purchase_date":order.get("purchase_date"),"shipping_status":status,
                       "shipping_status_source":"abcd_scenario" if status is not None else "missing_in_abcd",
                       "packaging":order.get("packaging"),"payment_method":order.get("payment_method"),
                       "num_products":int(order["num_products"]),"full_address":order.get("full_address"),
                       "street_address":order.get("street_address"),"city":order.get("city"),
                       "state":order.get("state"),"zip_code":order.get("zip_code")}
            orders.append(order_row)
            raw_items=ast.literal_eval(order["products"])
            for index,item in enumerate(raw_items):
                items.append({"order_id":order_id,"item_index":index,"name":scenario["product"]["names"][index],
                              "amount":scenario["product"]["amounts"][index],"brand":item.get("brand"),
                              "product_type":item.get("product_type"),"image_url":item.get("image_url")})
            case={"source_convo_id":c["convo_id"],"split":s,"subflow":intent,"customer_id":cid,
                  "order_id":order_id,"source_kind":"ABCD_with_synthetic_identity","reference_label_status":"not_created"}
            cases.append(case)
            messages=[]
            for raw_turn,dt in zip(c["original"],c["delexed"]):
                msg={"speaker":raw_turn[0],"text":rewrite_text(raw_turn[1],old,person)}
                if raw_turn[0]=="action":
                    msg["action"]=dt["targets"][2]
                    msg["params"]=[rewrite_text(p,old,person) if isinstance(p,str) else p for p in dt["targets"][3]]
                    if msg["action"]=="verify-identity" and len(msg["params"])>=2:
                        old_parts=old["customer_name"].split()
                        new_parts=person["customer_name"].split()
                        if len(old_parts)==len(new_parts)==2:
                            for part in (0,1):
                                if isinstance(msg["params"][part],str) and msg["params"][part].lower()==old_parts[part].lower():
                                    msg["params"][part]=new_parts[part]
                messages.append(msg)
            # Source names reused across cases; check no exact original identifier survives.
            rendered=[v for m in messages for v in [m["text"], *map(str, m.get("params", []))]]
            for key in ("customer_name","username","email","account_id","phone"):
                old_value=old.get(key)
                if old_value and old_value != person[key]:
                    for value in rendered:
                        if re.search(r"(?<![A-Za-z0-9])"+re.escape(str(old_value))+r"(?![A-Za-z0-9])",value,re.I):
                            source_identity_leak.append({"source_convo_id":c["convo_id"],"field":key,"context":value[:150]})
            mentioned_emails={value.lower() for m in messages for value in re.findall(
                r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",m["text"])}
            if mentioned_emails-{person["email"].lower()}:
                case["review_flags"]=["non_profile_email_in_source_dialogue"]
                identity_review_flags.append({"source_convo_id":c["convo_id"],"flag":"non_profile_email_in_source_dialogue"})
            else:case["review_flags"]=[]
            conversations.append({"source_convo_id":c["convo_id"],"split":s,"subflow":intent,
                                  "customer_id":cid,"scenario_personal":scenario_personal,
                                  "order_id":order_id,"messages":messages})
            refs.append({"source_convo_id":c["convo_id"],"subflow":intent,"order_id":order_id,
                         "shipping_status":status,"product_name":scenario["product"]["names"][0],
                         "review_flags":case["review_flags"]})
        group_rows.append({"customer_id":cid,"split":split,"member_level":tier,
                           "request_count":len(group),"request_types":[r["subflow"] for r in refs],"source_cases":refs})
    assert not source_identity_leak,source_identity_leak[:5]
    # A small multi-intent eval candidate set, not auto-labeled. All its order-change statuses must be known.
    samples=[]
    for split,n2,n3 in (("train",18,6),("dev",3,1),("test",3,1)):
        for count,wanted in ((2,n2),(3,n3)):
            eligible=[g for g in group_rows if g["split"]==split and g["request_count"]==count and
                      all(not r["review_flags"] for r in g["source_cases"]) and
                      all(r["shipping_status"] is not None for r in g["source_cases"] if FAMILY[r["subflow"]]=="manage")]
            RNG.shuffle(eligible)
            if len(eligible)<wanted:raise ValueError("not enough complete composites")
            for g in eligible[:wanted]:
                person=next(p for p in customers if p["customer_id"]==g["customer_id"])
                req=[{"intent":r["subflow"],"order_id":r["order_id"],"source_convo_id":r["source_convo_id"]} for r in g["source_cases"]]
                prompts=[request_phrase(r["subflow"],r["order_id"],r["product_name"]) for r in g["source_cases"]]
                samples.append({"case_id":f"multi-{g['customer_id']}","split":split,"customer_id":g["customer_id"],
                                "request_count":count,"requests":req,
                                "initial_customer_message":f"Hi, I'm {person['customer_name']}. I have {count} requests: " + " ".join(f"({j}) {prompt}" for j,prompt in enumerate(prompts,1)),
                                "message_source":"synthetic_template_from_ABCD_intents",
                                "reference_label_status":"not_created"})
    write_jsonl(OUT/"customers.jsonl",customers)
    write_jsonl(OUT/"orders.jsonl",orders)
    write_jsonl(OUT/"items.jsonl",items)
    write_jsonl(OUT/"source_cases.jsonl",cases)
    write_jsonl(OUT/"rewritten_conversations.jsonl",conversations)
    write_jsonl(OUT/"customer_request_groups.jsonl",group_rows)
    write_jsonl(OUT/"multi_request_cases.jsonl",samples)
    write_jsonl(OUT/"identity_review_flags.jsonl",identity_review_flags)
    build_sqlite(OUT/"synthetic_retail.sqlite",customers,orders,items,cases)
    manifest={"seed":SEED,"source_file":"raw_data/abcd_v1.1.json.gz",
              "source_sha256":hashlib.sha256((ROOT/"abcd_v1.1.json.gz").read_bytes()).hexdigest(),
              "source_selected_conversations":len(rows),"customers":len(customers),
              "customer_request_count_distribution":dict(Counter(g["request_count"] for g in group_rows)),
              "multi_request_case_candidates":len(samples),"splits":dict(Counter(p["split"] for p in customers)),
              "missing_shipping_status":sum(o["shipping_status"] is None for o in orders),
              "source_dialogues_with_non_profile_email":len(identity_review_flags),
              "source_identity_leaks":source_identity_leak,
              "notes":["Only identity values are synthetic; membership tier is held constant inside each linked customer.",
                       "ABCD conversations are separate historical role-play chats; multi_request_cases are new synthetic single-message tasks, not historical transcripts.",
                       "No reference labels are assigned. Generated JSONL and SQLite are checked into processed_data."]}
    (OUT/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(manifest,ensure_ascii=False,indent=2))

def build_sqlite(path,customers,orders,items,cases):
    if path.exists():path.unlink()
    con=sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript((PROJECT_ROOT / "sql" / "synthetic_retail_schema.sql").read_text(encoding="utf-8"))
    for table,rs in (("customers",customers),("orders",orders),("items",items),("source_cases",cases)):
        keys=list(rs[0])
        con.executemany(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                        ([json.dumps(r[k]) if k=="review_flags" else r[k] for k in keys] for r in rs))
    con.commit()
    if con.execute("PRAGMA foreign_key_check").fetchall():raise ValueError("foreign key violation")
    con.close()

if __name__=="__main__":build()
