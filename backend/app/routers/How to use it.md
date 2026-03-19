

---- hedge styles ----
- Section A:
Balanced
/api/hedge/plan?account_id=all:business&hedge_style=balanced
Cost sensitive
/api/hedge/plan?account_id=all:business&hedge_style=cost_sensitive
Crash paranoid
/api/hedge/plan?account_id=all:business&hedge_style=crash_paranoid
Correction focused
/api/hedge/plan?account_id=all:business&hedge_style=correction_focused

So:

- balanced will already lean primary
- correction_focused will lean even more primary
- crash_paranoid will keep more tail hedge than usual
- cost_sensitive will tone everything down


1.) Default.

uses regime split mostly as-is.
normal cost assumptions
normal coverage assumptions

2.) cost_sensitive
Cheaper hedge program.
slightly less primary hedge
slightly lower cost assumptions
slightly less expected coverage efficiency
Best for:users who hate hedge drag

3.) crash_paranoid

More tail protection.
shifts toward tail hedge
allows a bit more tail cost
assumes better tail usefulness
Best for: black swan protection mindset

4.) correction_focused

More primary hedge.
shifts toward 40D/20D
increases primary cost
reduces tail emphasis
Best for:10–20% correction defense
Practical intuition for your current regime
You are often in early_breakdown.

- If you call: /api/hedge/plan?account_id=all:business
The system will automatically choose hedge style based on regime.
Example:
If regime = early_breakdown; hedge_style = correction_focused
If regime = strong_bull; hedge_style = cost_sensitive

- Hedge/select?account_id=all:business
- hedge/roll?account_id=all:business
- hedge/reconcile?account_id=all:business
- hedge/orders?account_id=all:business&mode=preview mode can be dry_run or submit


--- Order Management ---

GET /api/hedge/orders/status

POST /api/hedge/orders/cancel

GET /api/hedge/orders/open

GET /api/hedge/orders/reconcile


Check all open hedge-related orders
http://localhost:8000/api/hedge/orders/status

Check all orders, not just open
http://localhost:8000/api/hedge/orders/status?open_only=false

Check a specific broker order id
http://localhost:8000/api/hedge/orders/status?broker_order_id=0562b5a5-cdb4-4505-9f8e-e5e5856eb3e9

Check by client order id
http://localhost:8000/api/hedge/orders/status?client_order_id=hedge-aa3bb89d5aed9fa9


Cancel by broker order id:

POST /api/hedge/orders/cancel?broker_order_id=6a0169bb-8810-4c6b-a28b-ac8c3f81e7db

Cancel by client order id:

POST /api/hedge/orders/cancel?client_order_id=hedge-66e9601a82501437

Anuj@Mac portfolio_dashboard % curl -X POST "http://localhost:8000/api/hedge/orders/cancel?broker_order_id=6a0169bb-8810-4c6b-a28b-ac8c3f81e7db"
{"broker":"alpaca","broker_environment":"paper","canceled":true,"broker_order_id":"6a0169bb-8810-4c6b-a28b-ac8c3f81e7db","client_order_id":null,"status":"cancel_requested","message":"Cancel accepted by Alpaca.","raw_response":{}}% 


----

All open Alpaca positions:

http://localhost:8000/api/hedge/positions/alpaca

Single symbol:

http://localhost:8000/api/hedge/positions/alpaca?symbol=QQQ260630P00475000

----


http://localhost:8000/api/hedge/holdings/unified?include_composer=true&include_alpaca=false

then:

http://localhost:8000/api/hedge/holdings/unified?include_composer=false&include_alpaca=true

then:

http://localhost:8000/api/hedge/holdings/unified?include_composer=true&include_alpaca=true


http://localhost:8000/api/hedge/holdings/unified?account_id=all

http://localhost:8000/api/hedge/holdings/unified?account_id=all&include_composer=true&include_alpaca=true




