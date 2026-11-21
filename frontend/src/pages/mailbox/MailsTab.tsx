import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import EmailsPage from '../adapter/email/emails'
import { mailboxApi, type MailboxScope } from '../../services/api/mailbox'

/**
 * 「邮件」标签：本实例自有邮箱收到的信
 *
 * 直接复用 Email 适配器页的邮件列表与详情（含正文隔离渲染、附件预览），只是带上实例范围：
 * 服务端按它只给本实例的缓存行，附件链接也补上实例参数。账户筛选只列本实例的邮箱。
 */
export default function MailsTab({ scope }: { scope: MailboxScope }) {
  const scopeKey = `${scope.adapter_key}|${scope.instance_key}`
  const accountsQuery = useQuery({
    queryKey: ['mailbox', 'accounts', scopeKey],
    queryFn: () => mailboxApi.listAccounts(scope),
    retry: false,
  })
  const accountOptions = useMemo(
    () =>
      (accountsQuery.data ?? []).map(account => ({
        value: account.username,
        label: account.display_name ? `${account.display_name} (${account.username})` : account.username,
      })),
    [accountsQuery.data]
  )
  return <EmailsPage scope={scope} accountOptions={accountOptions} />
}
