import type { AnnouncementInstanceRef } from '../../../../services/api/chat-channel'

/**
 * Stable identity of an adapter instance.
 *
 * `instance_key` is empty for single-instance adapters, so the pair
 * (adapter_key, instance_key) — not the instance key alone — identifies an instance.
 */
export const instanceRefKey = (instance: AnnouncementInstanceRef): string =>
  `${instance.adapter_key}::${instance.instance_key}`
