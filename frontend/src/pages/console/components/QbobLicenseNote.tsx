import { Link, Typography } from '@mui/material'
import { useTranslation } from 'react-i18next'

const QBOB_UPSTREAM_URL = 'https://github.com/Loping151/qbob'

/** qbob 是 AGPL-3.0 的独立项目：凡是展示它的界面都标明许可、上游地址与所用提交 */
export default function QbobLicenseNote({ qbobRef = '' }: { qbobRef?: string }) {
  const { t } = useTranslation('console-setup')
  return (
    <Typography variant="caption" color="text.secondary">
      {t('qbob.license')}{' '}
      <Link href={QBOB_UPSTREAM_URL} target="_blank" rel="noopener noreferrer">
        {QBOB_UPSTREAM_URL}
      </Link>
      {qbobRef ? (
        <>
          {' '}
          {t('qbob.licenseCommit')}{' '}
          <Link
            href={`${QBOB_UPSTREAM_URL}/commit/${qbobRef}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            {qbobRef.slice(0, 12)}
          </Link>
        </>
      ) : null}
    </Typography>
  )
}
