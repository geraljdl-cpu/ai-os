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
    import datetime
    date = params.get("date", str(datetime.date.today()))
    doc_lines = [{"description":l.get("description","Servico"),"quantity":l.get("quantity",1),"unit_price":l.get("unit_price",0),"tax_id":l.get("tax_id",1)} for l in lines]
    body = {"document_type":"FT","date":date,"finalize":0,"customer_id":int(customer_id),"notes":params.get("observations",""),"lines":doc_lines}
    import urllib.request as _ur
    data = __import__("json").dumps(body).encode()
    req = _ur.Request(BASE_URL+"/api/v1/commercial_sales_documents", data=data, headers={"Authorization":"Bearer "+get_token(),"Content-Type":"application/json","Accept":"application/json"})
    try:
        r = _ur.urlopen(req, timeout=15)
        return {"ok":True,"data":__import__("json").loads(r.read())}
    except _ur.error.HTTPError as e:
        return {"ok":False,"status":e.code,"error":e.read().decode()}
    except Exception as e:
        return {"ok":False,"error":str(e)}

def tool_toc_customer_create(params):
    body = {"data":{"type":"customers","attributes":{"business_name":params.get("name",""),"tax_registration_number":params.get("nif",""),"email":params.get("email",""),"phone_number":params.get("phone","")}}}
    return toc_post("/api/customers", body)

# EMERGENCY STOP — TOCONLINE COMPLETAMENTE DESATIVADO
TOOLS = {
    "toc_customers":     lambda p: {"ok": False, "error": "TOCONLINE DESATIVADO"},
    "toc_invoices":      lambda p: {"ok": False, "error": "TOCONLINE DESATIVADO"},
    "toc_invoice_create":lambda p: {"ok": False, "error": "TOCONLINE DESATIVADO"},
    "toc_customer_create":lambda p: {"ok": False, "error": "TOCONLINE DESATIVADO"},
}
if __name__ == "__main__":
    print('{"ok": false, "error": "TOCONLINE DESATIVADO"}')
