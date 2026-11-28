import { EmptyState } from "@angee/ui";

import { useMessagingImpactProbeT } from "../i18n";

export function MessagingImpactProbePage() {
  const t = useMessagingImpactProbeT();
  return <EmptyState fill icon="file" title={t("page.empty")} />;
}
