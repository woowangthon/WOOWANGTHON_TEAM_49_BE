"""HTTP, SQLite and real Agent coverage for service payroll regressions."""
import pytest
from app.db import dump
from app.compatibility import prepare_snapshot, schedule
from app.outputs import FieldMutationDTO
from test_review_policy import api as api_fixture, seed

from test_legacy_contract_restored import request, drain

api = api_fixture


@pytest.mark.parametrize("override", [False, True])
def test_resolution_wire_contract_and_persistent_review(api, override):
    c, app, wid, cid = api
    did, ids = seed(app, wid, cid, fields=[("memo", None, "MISSING")])
    base = f"/cases/{cid}/documents/{did}"
    with app.state.db.connect(write=True) as con:
        con.execute("UPDATE documents SET processing_status='REVIEW_REQUIRED' WHERE id=?", (did,))
    if override:
        with app.state.db.connect(write=True) as con:
            con.execute("INSERT INTO field_overrides(case_id,field_id,value_json,status) VALUES(?,?,?,'MISSING')", (cid, ids['memo'], dump(None)))
    for resolution in ("UNKNOWN", "NOT_IN_DOCUMENT", "NONE", "UNKNOWN"):
        body = request(c, "PATCH", base + f"/fields/{ids['memo']}", {"reviewResolution": resolution})
        FieldMutationDTO.model_validate(body)
        assert body['hasCaseOverride'] is override
        assert body['effectiveValueOrigin'] == ('CASE_OVERRIDE' if override else 'SHARED_DEFAULT')
        assert body['inputVersion'] and body['caseUpdates'][str(cid)]['inputVersion'] == body['inputVersion']
    assert request(c, "POST", base + "/review/confirm")['reviewStatus'] == 'CONFIRMED'
    with app.state.db.connect(write=True) as con:
        schedule(con, cid, delay=0, increment=False)
    drain(app)
    assert request(c, "GET", base + "/review")['reviewStatus'] == 'CONFIRMED'
    assert request(c, "GET", base + "/review")['processingStatus'] == 'REVIEW_REQUIRED'
    assert request(c, "GET", base)['reviewStatus'] == 'CONFIRMED'
    assert request(c, "GET", f"/cases/{cid}/documents")['items'][0]['reviewStatus'] == 'CONFIRMED'
    request(c, "GET", f"/cases/{cid}/readiness")
    request(c, "GET", f"/cases/{cid}/results")


def test_detail_rows_scope_and_recompute(api):
    c, app, wid, cid = api
    fields = [("gross", 3272727, "CONFIRMED"), ("deduction", 280000, "CONFIRMED"), ("net", 2992727, "CONFIRMED")]
    for prefix, values in (("earning", [2800000, 200000, 272727]), ("deduction", [120000, 100000, 60000])):
        for i, amount in enumerate(values, 1):
            fields.extend([(f"{prefix}_{i}_label", f"detail {i}", "UNCONFIRMED"), (f"{prefix}_{i}_amount", amount, "UNCONFIRMED")])
    did, ids = seed(app, wid, cid, "PAYSLIP", fields)
    base = f"/cases/{cid}/documents/{did}"
    with app.state.db.connect(write=True) as con:
        snapshot, *_ = prepare_snapshot(con, cid, 'rows')
        assert len(snapshot.earnings) == len(snapshot.deductions) == 3
        assert not snapshot.earnings_complete
        schedule(con, cid, delay=0)
    drain(app)
    assert request(c, 'GET', base + '/statement-scope')['scopeStatus']['paymentItems'] == 'UNCONFIRMED'
    result = request(c, "GET", f"/cases/{cid}/results")
    assert 'ITEM_SCOPE_INCOMPLETE' in result['payslipValidation']['paymentTotal']['reasonCodes']
    scope = request(c, "PUT", base + '/statement-scope', {'paymentItems': True, 'deductionItems': True})
    assert scope['scopeStatus'] == {'paymentItems': 'CONFIRMED', 'deductionItems': 'CONFIRMED'}
    assert request(c, 'GET', base + '/statement-scope')['scopeStatus'] == scope['scopeStatus']
    drain(app)
    result = request(c, "GET", f"/cases/{cid}/results")['payslipValidation']
    for name, total in (('paymentTotal', 3272727), ('deductionTotal', 280000), ('netPay', 2992727)):
        assert result[name]['status'] == 'MATCH', result
        assert result[name]['itemSum'] == total
    with app.state.db.connect(write=True) as con:
        snapshot, *_ = prepare_snapshot(con, cid, 'attested')
        assert all(not i.confirmed and i.analysis_value_origin == 'SYSTEM_EXTRACTED_UNVERIFIED' for i in snapshot.earnings)
    request(c, 'PATCH', base + f"/fields/{ids['earning_3_amount']}", {'value': 1})
    assert request(c, 'GET', base + '/statement-scope')['scopeStatus']['paymentItems'] == 'NEEDS_RECONFIRMATION'
    drain(app)
    assert request(c, 'GET', f'/cases/{cid}/results')['payslipValidation']['paymentTotal']['itemSum'] is None
    request(c, 'PUT', base + '/statement-scope', {'paymentItems': True})
    drain(app)
    assert request(c, 'GET', f'/cases/{cid}/results')['payslipValidation']['paymentTotal']['status'] == 'DIFFERENCE'


def test_unconfirmed_payment_never_counted(api):
    c, app, wid, cid = api
    seed(app, wid, cid, 'PAYSLIP', [('net', 900, 'CONFIRMED')])
    tx = request(c, 'POST', f'/workplaces/{wid}/payment-transactions', {'paymentMethod': 'BANK_TRANSFER', 'transactionDate': '2026-10-31', 'amount': 900, 'source': 'USER_INPUT', 'confirmationStatus': 'USER_REPORTED'}, 201)
    request(c, 'POST', f'/cases/{cid}/deposits', {'transactionId': tx['transactionId'], 'allocatedAmount': 900}, 201)
    drain(app)
    payment = request(c, 'GET', f'/cases/{cid}/results')['depositComparison']
    assert payment['linkedPaymentTotal'] is None
    assert 'PAYMENT_SCOPE_UNCONFIRMED' in payment['reasonCodes']
    request(c, 'PATCH', f"/workplaces/{wid}/payment-transactions/{tx['transactionId']}", {'confirmationStatus': 'USER_CONFIRMED'})
    drain(app)
    assert request(c, 'GET', f'/cases/{cid}/results')['depositComparison']['linkedPaymentTotal'] == 900


def test_openapi_mutation_fields(api):
    client, _, _, _ = api
    schema = client.get('/openapi.json').json()['components']['schemas']
    required = schema['FieldMutationDTO']['required']
    assert {'inputVersion', 'recompute', 'hasCaseOverride', 'effectiveValueOrigin', 'value', 'reviewResolution'} <= set(required)
    assert 'does not create' in schema['FieldMutationDTO']['properties']['hasCaseOverride']['description']


def test_value_shared_override_removal_contract(api):
    c, app, wid, cid = api
    did, ids = seed(app, wid, cid, fields=[('hourlyWage', 10000, 'CONFIRMED')])
    path = f'/cases/{cid}/documents/{did}'
    mutation = request(c, 'PATCH', path + f"/fields/{ids['hourlyWage']}", {'value': 11000, 'confirmed': True})
    FieldMutationDTO.model_validate(mutation)
    assert mutation['hasCaseOverride'] and mutation['value'] == 11000
    shared = request(c, 'PATCH', f"/workplaces/{wid}/documents/{did}/fields/{ids['hourlyWage']}", {'value': 12000, 'confirmed': True})
    assert not shared['hasCaseOverride'] and shared['effectiveValueOrigin'] == 'SHARED_DEFAULT'
    removed = request(c, 'DELETE', path + f"/field-overrides/{ids['hourlyWage']}")
    FieldMutationDTO.model_validate(removed)
    assert not removed['hasCaseOverride'] and removed['value'] == 12000


def test_monthly_rate_unit_mapping():
    from app.compatibility import public_field
    monthly = public_field({'key': 'contract_rate', 'value': '2800000', 'status': 'CONFIRMED', 'source': {'unit': 'KRW/month'}})
    assert monthly['key'] == 'monthlyBasePay' and monthly['value'] == 2800000
    annual = public_field({'key': 'contract_rate', 'value': '33600000', 'status': 'CONFIRMED', 'source': {'unit': 'KRW/year'}})
    assert annual['status'] == 'UNREADABLE' and annual['value'] is None
