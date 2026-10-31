"""Native composed-host contracts for the method-only Discord donor."""

from django.apps import apps
from django.test import SimpleTestCase
from rebac.resources import model_resource_type

from angee.messaging_integrate_discord.models import DiscordChannelState


class DiscordCompositionTests(SimpleTestCase):
    def test_channel_discovers_provider_without_replacing_native_identity(self):
        channel = apps.get_model("messaging", "Channel")
        self.assertIn(DiscordChannelState, channel.__mro__)
        self.assertFalse(channel._meta.proxy)
        self.assertEqual(model_resource_type(channel), "messaging/channel")
        self.assertTrue(callable(channel.objects.named_import_channel))
        self.assertTrue(callable(channel.objects.unfinished_pairing))
        self.assertEqual(channel._meta.get_field("subscription_state").model._meta.label, "messaging.Channel")
