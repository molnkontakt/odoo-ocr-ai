"""The background job reads a bill as the user who queued it (#9, #29).

With that user's access rights (a user who may not create contacts gets no new vendor, as
with the form button), in that user's language, with the bill's company; the notes have
that user as author. Only the queue's bookkeeping runs with superuser rights. A user who is
archived or lost the company is not replaced by OdooBot: the bill fails with the reason.
"""
from unittest import mock

from odoo import Command
from odoo.tests import new_test_user, tagged
from odoo.tools import mute_logger

from .common import OcrBillCase, run_ocr_cron
from .test_ocr_savepoint import AI_NEW_VENDOR, TEXT_NEW_VENDOR

BILLING = "base.group_user,account.group_account_invoice"


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrQueueUser(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("invoice_ocr.max_attempts", "1")
        cls.env["res.lang"]._activate_lang("fr_FR")
        company = cls.env.company
        cls.user = new_test_user(cls.env, "ocr_billing", groups=BILLING, lang="fr_FR",
                                 company_id=company.id, company_ids=[Command.set(company.ids)])

    def _spy(self):
        """Records the environment _ocr_queue_read runs in."""
        Move = type(self.env["account.move"])
        original = Move._ocr_queue_read
        seen = []

        def spy(record, final=True):
            seen.append({"uid": record.env.uid, "su": record.env.su, "lang": record.env.lang,
                         "companies": record.env.companies.ids})
            return original(record, final=final)

        return mock.patch.object(Move, "_ocr_queue_read", spy), seen

    def _read_queued(self, move):
        patch, seen = self._spy()
        with patch, self._patch_ocr(TEXT_NEW_VENDOR, AI_NEW_VENDOR), \
                mute_logger("odoo.addons.account_invoice_ocr_ai.models.ocr_queue"):
            run_ocr_cron(self.env)
        return seen

    def _newcomers(self):
        return self.env["res.partner"].search([("name", "=", "Example Newcomer AB")])

    def test_read_as_the_user_who_queued_it(self):
        move = self._new_bill()
        self._upload(move.with_user(self.user))
        self.assertEqual(move.ocr_requested_by, self.user)
        seen = self._read_queued(move)
        self.assertEqual(seen, [{"uid": self.user.id, "su": False, "lang": "fr_FR",
                                 "companies": [move.company_id.id]}])
        self.assertEqual(move.ocr_state, "done")
        self.assertEqual(move.ref, "4711")
        # no partner-create rights: no vendor, a note instead — as with the form button
        self.assertFalse(self._newcomers())
        self.assertFalse(move.partner_id)
        self.assertIn("you may not create contacts", self._bodies(move))
        fill = move.message_ids.filtered(lambda m: m.subject == "OCR fill")
        self.assertEqual(fill.author_id, self.user.partner_id)

    def test_a_user_who_may_create_contacts_creates_the_vendor(self):
        self.user.group_ids = [Command.link(self.env.ref("base.group_partner_manager").id)]
        move = self._new_bill()
        self._upload(move.with_user(self.user))
        self._read_queued(move)
        self.assertEqual(move.partner_id, self._newcomers())
        self.assertEqual(move.partner_id.create_uid, self.user)

    def test_archived_user_fails_without_reading(self):
        move = self._new_bill()
        self._upload(move.with_user(self.user))
        self.user.active = False
        seen = self._read_queued(move)
        self.assertEqual(seen, [], "not read as OdooBot either")
        self.assertEqual(move.ocr_state, "failed")
        self.assertIn("ocr_billing", move.ocr_error)
        self.assertIn("is archived", move.ocr_error)

    def test_user_without_the_company_fails(self):
        other = self.env["res.company"].create({"name": "Example Other Company"})
        self.user.company_ids = [Command.link(other.id)]
        move = self._new_bill()
        self._upload(move.with_user(self.user))
        self.user.write({"company_id": other.id, "company_ids": [Command.set(other.ids)]})
        seen = self._read_queued(move)
        self.assertEqual(seen, [])
        self.assertEqual(move.ocr_state, "failed")
        self.assertIn("no longer has access to the company", move.ocr_error)

    def test_emailed_bill_is_read_as_its_sender(self):
        """The mail gateway queues as OdooBot: the sender, when a user, is the reader."""
        move = self._new_bill()
        self.env["mail.message"].create({
            "model": "account.move", "res_id": move.id, "message_type": "email",
            "author_id": self.user.partner_id.id, "body": "Bill attached"})
        self._upload(move)  # as the superuser, like the mail gateway
        self.assertEqual(move.ocr_requested_by, self.user)

    def test_bill_from_an_unknown_sender_is_read_as_odoobot(self):
        stranger = self.env["res.partner"].create({"name": "Example Stranger",
                                                   "email": "stranger@example.com"})
        move = self._new_bill()
        self.env["mail.message"].create({
            "model": "account.move", "res_id": move.id, "message_type": "email",
            "author_id": stranger.id, "body": "Bill attached"})
        self._upload(move)
        self.assertFalse(move.ocr_requested_by)
        seen = self._read_queued(move)
        self.assertTrue(seen[0]["su"])
        self.assertEqual(move.ocr_state, "done")
        self.assertEqual(move.partner_id, self._newcomers())
