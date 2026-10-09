from rest_framework import serializers as s

DOC_EXAMPLE = {"id": "3b1f8c52-7a1d-4d0e-9d58-2c6a1b7e4f90", "document_type": "claim_form",
               "file": "https://files.theeinsurance.com/claims/documents/claim-form.pdf",
               "uploaded_at": "2026-10-06T10:20:00Z"}

PAYMENT_EXAMPLE = {"reference": "CLMPAY-CLM-9F3A1B7C2D4E5F60-1", "amount": "120000.00", "status": "completed",
                   "receipt_url": "https://receipts.theeinsurance.com/CLMPAY-1.pdf", "account_name": "Chidi Eze",
                   "account_number_masked": "******6789", "created_at": "2026-10-07T09:00:00Z",
                   "completed_at": "2026-10-07T09:01:12Z"}

CLAIM_EXAMPLE = {
    "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "claim_reference": "CLM-9F3A1B7C2D4E5F60",
    "subscription": "a3c1e5f2-9b7d-4c3a-8e1f-6d2b0c9a4e71", "plan_name": "Travel Plus",
    "customer": "0d5c7c1e-61f2-4a6b-9a3e-5b1c2f9e8a10", "customer_email": "chidi@example.com",
    "provider": "3f6c1f4e-8a58-4b6e-9a53-2d6a7f0d9c11", "provider_name": "Sunrise Assurance Plc",
    "claim_type": "travel_baggage", "incident_date": "2026-09-28",
    "incident_description": "Checked bag did not arrive in Lagos. PIR filed at the airport.",
    "claimed_amount": "150000.00", "approved_amount": None, "status": "forwarded",
    "forwarded_at": "2026-10-06T10:30:00Z", "theeinsurance_review_note": "", "provider_review_note": "",
    "documents": [DOC_EXAMPLE], "payments": [], "settlement_on_file": True,
    "submitted_at": "2026-10-06T10:00:00Z", "updated_at": "2026-10-06T10:30:00Z",
}
AI_RESULT_EXAMPLE = {"score": 0.12, "flags": [], "summary": "Documents consistent with the incident.",
                     "recommendation": "forward"}
CLAIM_STAFF_EXAMPLE = {**CLAIM_EXAMPLE, "ai_result": AI_RESULT_EXAMPLE, "parties_notified_at": None}

CLAIM_CREATED_201 = {
    "message": "Claim submitted. Please upload supporting documents.",
    "claim_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "claim_reference": "CLM-9F3A1B7C2D4E5F60",
    "required_documents": ["claim_form", "property_irregularity_report", "flight_documents",
                           "travel_insurance_certificate"],
    "settlement_on_file": False,
    "next_steps": {"upload_documents": "POST /api/v1/claims/7c9e6679-7425-40de-944b-e07fc1f90ae7/documents/",
                   "add_settlement_account": "PATCH /api/v1/auth/profile/"},
}

CREATE_REQ_EXAMPLE = {"subscription_id": "a3c1e5f2-9b7d-4c3a-8e1f-6d2b0c9a4e71", "claim_type": "travel_baggage",
                      "incident_date": "2026-09-28",
                      "incident_description": "Checked bag did not arrive in Lagos. PIR filed at the airport.",
                      "claimed_amount": "150000.00"}

UploadResponse = s.Serializer  # placeholder so imports stay tidy; real schemas are inline in views
