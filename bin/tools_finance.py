#!/usr/bin/env python3
import os, json, datetime, urllib.request, urllib.parse

TOKEN_FILE = os.path.expanduser("~/ai-os/.toc_token.json")
BASE_URL = "https://api29.toconline.pt"

def get_token():
    return json.load(open(TOKEN_FILE))["access_token"]

def toc_get(path):
    req = urllib.request.Request(BASE_URL+path, headers={"Authorization":"Bearer "+get_token(),"Accept":"application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return {"ok":True,"data":json.loads(r.read())}
    except urllib.error.HTTPError as e:
        return {"ok":False,"status":e.code,"error":e.read().decode()}
    except Exception as e:
        return {"ok":False,"error":str(e)}

def toc_post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE_URL+path, data=data, headers={"Authorization":"Bearer "+get_token(),"Content-Type":"application/vnd.api+json","Accept":"application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return {"ok":True,"data":json.loads(r.read())}
    except urllib.error.HTTPError as e:
        return {"ok":False,"status":e.code,"error":e.read().decode()}
    except Exception as e:
        return {"ok":False,"error":str(e)}

def tool_toc_customers(params):
    limit = params.get("limit", 20)
    search = params.get("search","")
    path = f"/api/customers?page[size]={limit}"
    if search: path += "&filter[search]="+urllib.parse.quote(search)
    r = toc_get(path)
    if not r["ok"]: return r
    raw = r["data"].get("data", r["data"]) if isinstance(r["data"], dict) else r["data"]
    out = []
    for c in raw:
        a = c.get("attributes", c)
        out.append({"id":c.get("id",""),"name":a.get("business_name",""),"nif":a.get("tax_registration_number",""),"email":a.get("email","")})
    return {"ok":True,"count":len(out),"customers":out}

def tool_toc_invoices(params):
    limit = params.get("limit", 20)
    r = toc_get(f"/api/v1/commercial_sales_documents?page[size]={limit}")
    if not r["ok"]: return r
    raw = r["data"] if isinstance(r["data"], list) else r["data"].get("data",[])
    out = []
    for d in raw:
        out.append({"id":d.get("id",""),"number":str(d.get("document_series_prefix",""))+"/"+str(d.get("document_series_no","")),"date":d.get("date",""),"total":d.get("gross_total",""),"customer_id":d.get("customer_id",""),"type":d.get("document_area","")})
    return {"ok":True,"count":len(out),"invoices":out}

def tool_toc_invoice_create(params):
    customer_id = params.get("customer_id")
    lines = params.get("lines",[])
    if not customer_id: return {"ok":False,"error":"customer_id obrigatorio"}
    if not lines: return {"ok":False,"error":"lines obrigatorio"}
    date = params.get("date", str(datetime.date.today()))
    doc_lines = [{"description":l.get("description","Servico"),"quantity":l.get("quantity",1),"unit_price":l.get("unit_price",0),"tax_rate":l.get("tax_rate",23),"discount":l.get("discount",0)} for l in lines]
    body = {"data":{"type":"commercial_documents","attributes":{"document_type":"FT","date":date,"customer_id":int(customer_id),"observations":params.get("observations",""),"lines":doc_lines}}}
    return toc_post("/api/v1/commercial_sales_documents", body)

def tool_toc_customer_create(params):
    body = {"data":{"type":"customers","attributes":{"business_name":params.get("name",""),"tax_registration_number":params.get("nif",""),"email":params.get("email",""),"phone_number":params.get("phone","")}}}
    return toc_post("/api/customers", body)

TOOLS = {"toc_customers":tool_toc_customers,"toc_invoices":tool_toc_invoices,"toc_invoice_create":tool_toc_invoice_create,"toc_customer_create":tool_toc_customer_create}

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv)>1 else "toc_customers"
    p = json.loads(sys.argv[2]) if len(sys.argv)>2 else {}
    fn = TOOLS.get(cmd)
    if not fn: print(json.dumps({"ok":False,"error":"tool desconhecida: "+cmd}))
    else: print(json.dumps(fn(p), indent=2, ensure_ascii=False))
