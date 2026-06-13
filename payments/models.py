"""
Payments Database Models Module.

This module defines the database schema and business logic for tracking 
financial transactions, premium collections, and gateway integrations
"""

import uuid
import secrets
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _


def generate_reference() -> str:
    """
    Generates a secure, unique, and cryptographically random transaction reference.
    """

    return f"TII-{secrets.token_hex(8).upper()}"


class Transaction(models.Model):
    """
    Represents a financial ledger entry for policy premium payments.

    This model tracks the entire lifecycle of a payment attempt within the 
    insurance portal—from initiation to gateway verification.
    """
    
    class PAYMENT_STATUS(models.TextChoices):
        """
        Payment statuses for all transactions
        """
        
        PENDING = 'PENDING', _('Pending')
        SUCCESSFUL = 'SUCCESSFUL', _('Successful')
        FAILED = 'FAILED', _('Failed')
        REVERSED = 'REVERSED', _('Reversed')


    class PAYMENT_TYPE(models.TextChoices):
        """
        Payment types depending on customers goal
        """
        
        NEW_SUBSCRIPTION = 'NEW_SUBSCRIPTION', _('New Subscription')
        RENEWAL = 'RENEWAL', _('Renewal')

    id: models.UUIDField = models.UUIDField(
        primary_key=True, 
        default=uuid.uuid4, 
        editable=False
    )

    reference: models.CharField = models.CharField(
        max_length=100, 
        unique=True,
        editable=False,
        default=generate_reference,
    )

    amount: models.DecimalField = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        editable=False
    )
    
    currency: models.CharField = models.CharField(
        max_length=3, 
        default='NGN'
    )
    
    initiated_by: models.ForeignKey = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='initiated_transactions'
    )
    
    subscription: models.ForeignKey = models.ForeignKey(
        'subscriptions.PolicySubscription',
        on_delete=models.PROTECT,
        related_name='transactions'
    )
    
    payment_type: models.CharField = models.CharField(
        max_length=30,
        choices=PAYMENT_TYPE.choices
    )

    payment_status: models.CharField = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS.choices,
        default=PAYMENT_STATUS.PENDING,
    )
    
    gateway_reference: models.CharField = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True
    )

    gateway_response: models.JSONField = models.JSONField(
        null=True,
        blank=True,
    )

    created_at: models.DateTimeField = models.DateTimeField(
        auto_now_add=True, db_index=True
    )

    updated_at: models.DateTimeField = models.DateTimeField(
        auto_now=True
    )
    
    class Meta:
        db_table = 'transactions'
        ordering = ['-created_at']
        verbose_name = _('transaction')
        verbose_name_plural = _('transactions')

    def __str__(self) -> str:
        return f"Txn {self.reference} ({self.payment_status})"


class CallbackLog(models.Model):
    """
    Immutable audit record of every Payment Gateway callback we receive.
    Created inside the same atomic transaction as the Transaction state update,
    so the log is always consistent with the transaction outcome.
    """

    class Status(models.TextChoices):
        SUCCESS = 'SUCCESS', _('Success')
        FAILED = 'FAILED', _('Failed')
        FLAGGED = 'FLAGGED', _('Flagged')

    uuid: models.UUIDField = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )
    
    transaction_reference: models.CharField = models.CharField(
        max_length=255,
        db_index=True
    )

    raw_payload: models.JSONField = models.JSONField()
    
    response_code: models.CharField = models.CharField(
        max_length=20,
        blank=True,
        default=''
    )
    
    status: models.CharField = models.CharField(
        max_length=20,
        choices=Status.choices,
        db_index=True
    )
    
    transaction: models.ForeignKey = models.ForeignKey(
        Transaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='callback_logs',
    )
    
    is_duplicate: models.BooleanField = models.BooleanField(
        default=False,
        db_index=True
    )

    is_amount_mismatch: models.BooleanField = models.BooleanField(
        default=False,
        db_index=True
    )

    created_at: models.DateTimeField = models.DateTimeField(
        auto_now_add=True, db_index=True
    )

    updated_at: models.DateTimeField = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        db_table = 'transaction_callback_logs'
        ordering = ['-created_at']
        verbose_name = _('callback log')
        verbose_name_plural = _('callback logs')
        unique_together = ('transaction_reference', 'response_code', 'status')

    def __str__(self):
        return f"CallbackLog {self.transaction_reference} — {self.status}"
