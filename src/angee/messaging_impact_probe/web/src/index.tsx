import { defineBaseAddon, resourcePageRoutes } from "@angee/app";
import { lazyRouteComponent } from "@tanstack/react-router";

import {
  enMessagingImpactProbeMessages,
} from "./i18n";

const addon = defineBaseAddon({
  id: "messaging_impact_probe",
  // The route claims the canonical `<label>.<Model>` resource label: durable
  // manifests never store a bare model name.
  routes: resourcePageRoutes(
    "messaging_impact_probe.messagingimpactprobe",
    "/messaging-impact-probe",
    lazyRouteComponent(() => import("./views/MessagingImpactProbePage"), "MessagingImpactProbePage"),
    "messaging_impact_probe.MessagingImpactProbe",
  ),
  menus: {
    "messaging_impact_probe": {
      label: "Messaging Impact Probe",
      icon: "file",
      route: "messaging_impact_probe.messagingimpactprobe",
    },
  },
  i18n: {
    "messaging_impact_probe": enMessagingImpactProbeMessages,
  },
});

export default addon;
