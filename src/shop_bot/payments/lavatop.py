from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum

import requests
from lava_top_sdk import Currency, InvoicePaymentParamsResponse, LavaClient, LavaClientConfig, Periodicity, WebhookEventType
from lava_top_sdk.types_custom import ClientUtm, InvoiceRequestDto

from shop_bot.data_manager.database import get_setting


SUCCESS_EVENT_TYPES = {
    WebhookEventType.PAYMENT_SUCCESS,
    WebhookEventType.SUBSCRIPTION_RECURRING_PAYMENT_SUCCESS,
}


class LavaTopError(Exception):
    pass


class LavaTopConfigError(LavaTopError):
    pass


class LavaTopRequestError(LavaTopError):
    pass


class TariffCode(str, Enum):
    MONTH_1 = "one_month"
    MONTH_3 = "three_month"
    MONTH_6 = "six_month"
    MONTH_12 = "twelveteen_month"


@dataclass(frozen=True)
class TariffPlan:
    code: TariffCode
    amount_rub: int
    offer_id: str


@dataclass(frozen=True)
class InvoiceResult:
    payment_url: str
    invoice_id: str | None = None


class LavaTopService:
    @staticmethod
    def _api_key() -> str:
        return (get_setting("lava_api_key") or "").strip()

    @staticmethod
    def _api_url() -> str:
        return (get_setting("lava_api_url") or "").strip().rstrip("/")

    @staticmethod
    def _env() -> str:
        return (get_setting("lava_env") or "production").strip() or "production"

    @staticmethod
    def _timeout_sec() -> int:
        raw = (get_setting("lava_timeout_sec") or "15").strip()
        try:
            return max(5, int(raw))
        except (TypeError, ValueError):
            return 15

    @staticmethod
    def _max_retries() -> int:
        raw = (get_setting("lava_max_retries") or "3").strip()
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 3

    @classmethod
    def _validate_base_config(cls) -> None:
        if not cls._api_key():
            raise LavaTopConfigError("Не задан lava_api_key")
        if not cls._api_url():
            raise LavaTopConfigError("Не задан lava_api_url")

    @staticmethod
    def _tariff_for_months(months: int, amount_rub: int) -> TariffPlan:
        months_int = int(months)
        amount_int = int(round(float(amount_rub)))
        if months_int == 1:
            offer_id = (get_setting("lava_offer_id_1_month") or "").strip()
            code = TariffCode.MONTH_1
        elif months_int == 3:
            offer_id = (get_setting("lava_offer_id_3_months") or "").strip()
            code = TariffCode.MONTH_3
        elif months_int == 6:
            offer_id = (get_setting("lava_offer_id_6_months") or "").strip()
            code = TariffCode.MONTH_6
        elif months_int == 12:
            offer_id = (get_setting("lava_offer_id_12_months") or "").strip()
            code = TariffCode.MONTH_12
        else:
            raise LavaTopConfigError("Lava.top поддерживает только тарифы на 1, 3, 6 и 12 месяцев")

        if not offer_id:
            raise LavaTopConfigError(f"Не задан offer_id для тарифа на {months_int} мес.")

        return TariffPlan(code=code, amount_rub=amount_int, offer_id=offer_id)

    @classmethod
    def _build_client(cls) -> LavaClient:
        cls._validate_base_config()
        return LavaClient(
            LavaClientConfig(
                api_key=cls._api_key(),
                env=cls._env(),
                base_url=cls._api_url(),
                timeout=cls._timeout_sec(),
                max_retries=cls._max_retries(),
            )
        )

    @classmethod
    def _post_invoice(cls, payload: dict) -> InvoicePaymentParamsResponse:
        response = requests.post(
            f"{cls._api_url()}/api/v2/invoice",
            json=payload,
            headers={
                "X-Api-Key": cls._api_key(),
                "Content-Type": "application/json",
            },
            timeout=cls._timeout_sec(),
        )

        if response.status_code >= 400:
            raise LavaTopRequestError(f"Lava HTTP {response.status_code}: {response.text[:1000]}")

        return InvoicePaymentParamsResponse(**response.json())

    @classmethod
    async def create_invoice(
        cls,
        *,
        email: str,
        months: int,
        amount_rub: int,
        telegram_user_id: int,
        username: str | None,
    ) -> InvoiceResult:
        cls._validate_base_config()
        tariff = cls._tariff_for_months(months=months, amount_rub=amount_rub)

        payload = InvoiceRequestDto(
            email=email,
            offerId=tariff.offer_id,
            currency=Currency.RUB,
            periodicity=Periodicity.ONE_TIME,
            clientUtm=ClientUtm(
                utm_source="telegram_bot",
                utm_medium="telegram",
                utm_campaign=tariff.code.value,
                utm_term=username or "",
                utm_content=f"tg_user_id:{telegram_user_id}",
            ),
        ).model_dump(exclude_none=True)

        try:
            response = await asyncio.to_thread(cls._post_invoice, payload)
        except LavaTopError:
            raise
        except Exception as exc:
            raise LavaTopRequestError(f"Не удалось создать ссылку на оплату через Lava.top: {exc}") from exc

        if not response.paymentUrl:
            raise LavaTopRequestError("Lava.top не вернул ссылку на оплату")

        return InvoiceResult(payment_url=response.paymentUrl, invoice_id=response.id)

    @classmethod
    def parse_success_webhook(cls, payload: str) -> tuple[str | None, dict]:
        cls._validate_base_config()
        try:
            payload_data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LavaTopRequestError("Webhook содержит невалидный JSON") from exc

        event = cls._build_client().parse_webhook(payload_data)
        if event.eventType not in SUCCESS_EVENT_TYPES:
            return None, payload_data

        return event.contractId, payload_data
