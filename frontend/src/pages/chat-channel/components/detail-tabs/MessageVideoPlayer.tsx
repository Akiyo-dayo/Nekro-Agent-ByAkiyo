import { useCallback, useRef, useState } from 'react'
import { Box, CircularProgress, Typography, useTheme } from '@mui/material'
import PlayCircleOutlineIcon from '@mui/icons-material/PlayCircleOutline'
import ReplayIcon from '@mui/icons-material/Replay'
import VideoFileIcon from '@mui/icons-material/VideoFile'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../../../components/common/ActionButton'
import { chatChannelApi, ChatMessageSegment, MessageVideoAccess } from '../../../../services/api/chat-channel'
import { formatBytes } from '../../../../services/api/space-cleanup'

type VideoErrorKey = Exclude<MessageVideoAccess['status'], 'ok'> | 'playback'

type PlayerState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'ready'; src: string; attempt: number }
  | { phase: 'error'; key: VideoErrorKey }

const VIDEO_PLACEHOLDER = /^\[Video: message:(-?\d+)\]$/

/** 占位段里的编号指向视频所在的消息；引用回复带出来的占位指向被引用的那条，不是当前消息 */
function videoMessageId(segment: ChatMessageSegment, fallbackMessageId?: string): string {
  return VIDEO_PLACEHOLDER.exec(segment.text)?.[1] ?? fallbackMessageId ?? ''
}

/**
 * OneBot 视频的内嵌播放器
 *
 * 点开才加载：每次探测都要问协议端、再向 QQ 要一个字节，不该在滚动消息列表时发生。
 * `<video>` 的 `onError` 读不到响应体，所以先走 `video-access`，放不了的原因在这里拿到。
 * 播放中出错多半是签名到期，重新取一次地址，从原来的位置接着放；再失败才显示提示。
 */
export default function MessageVideoPlayer({
  chatKey,
  segment,
  fallbackMessageId,
  isDark,
}: {
  chatKey: string
  segment: ChatMessageSegment
  fallbackMessageId?: string
  isDark: boolean
}) {
  const { t } = useTranslation('chat-channel')
  const theme = useTheme()
  const [state, setState] = useState<PlayerState>({ phase: 'idle' })
  const [fileName, setFileName] = useState(segment.video_name ?? '')
  const [fileSize, setFileSize] = useState(segment.file_size ?? 0)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const attemptRef = useRef(0)
  const retriedRef = useRef(false)
  const resumeAtRef = useRef(0)
  const messageId = videoMessageId(segment, fallbackMessageId)

  const load = useCallback(async () => {
    if (!messageId) {
      setState({ phase: 'error', key: 'unavailable' })
      return
    }
    setState({ phase: 'loading' })
    try {
      const access = await chatChannelApi.getMessageVideoAccess(chatKey, messageId)
      if (access.status !== 'ok') {
        setState({ phase: 'error', key: access.status })
        return
      }
      setFileName(current => current || access.file_name)
      setFileSize(current => current || access.file_size)
      attemptRef.current += 1
      setState({ phase: 'ready', src: access.url, attempt: attemptRef.current })
    } catch {
      setState({ phase: 'error', key: 'unavailable' })
    }
  }, [chatKey, messageId])

  const handleVideoError = () => {
    if (retriedRef.current) {
      setState({ phase: 'error', key: 'playback' })
      return
    }
    retriedRef.current = true
    resumeAtRef.current = videoRef.current?.currentTime ?? 0
    void load()
  }

  const handleLoadedMetadata = () => {
    if (videoRef.current && resumeAtRef.current > 0) {
      videoRef.current.currentTime = resumeAtRef.current
      resumeAtRef.current = 0
    }
  }

  const title = fileName || t('messageHistory.video.title')
  const sizeText = fileSize > 0 ? formatBytes(fileSize, 1) : ''

  return (
    <Box
      sx={{
        my: 0.5,
        p: 1,
        borderRadius: '10px',
        bgcolor: isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.03)',
        border: `1px solid ${theme.palette.divider}`,
        width: 320,
        maxWidth: '100%',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5, minWidth: 0 }}>
        <VideoFileIcon sx={{ fontSize: 20, color: theme.palette.error.main, flexShrink: 0 }} />
        <Typography
          variant="body2"
          title={title}
          sx={{
            flex: 1,
            minWidth: 0,
            fontSize: '13px',
            fontWeight: 500,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {segment.quoted ? `${t('messageHistory.video.quoted')} · ${title}` : title}
        </Typography>
        {sizeText && (
          <Typography variant="caption" sx={{ fontSize: '11px', color: theme.palette.text.disabled, flexShrink: 0 }}>
            {sizeText}
          </Typography>
        )}
      </Box>

      {state.phase === 'ready' ? (
        <Box
          component="video"
          key={state.attempt}
          ref={videoRef}
          src={state.src}
          controls
          autoPlay
          playsInline
          preload="metadata"
          onError={handleVideoError}
          onLoadedMetadata={handleLoadedMetadata}
          onPlaying={() => {
            retriedRef.current = false
          }}
          sx={{ display: 'block', width: '100%', maxHeight: 360, borderRadius: 1, bgcolor: 'common.black' }}
        />
      ) : (
        <Box
          sx={{
            height: 120,
            borderRadius: 1,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 1,
            px: 2,
            textAlign: 'center',
            bgcolor: isDark ? 'rgba(0,0,0,0.25)' : 'rgba(0,0,0,0.05)',
          }}
        >
          {state.phase === 'loading' && <CircularProgress size={24} />}
          {state.phase === 'idle' && (
            <ActionButton tone="ghost" size="small" startIcon={<PlayCircleOutlineIcon />} onClick={() => void load()}>
              {t('messageHistory.video.play')}
            </ActionButton>
          )}
          {state.phase === 'error' && (
            <>
              <Typography variant="caption" sx={{ color: theme.palette.text.secondary }}>
                {t(`messageHistory.video.error.${state.key}`)}
              </Typography>
              {state.key !== 'recalled' && (
                <ActionButton
                  tone="ghost"
                  size="small"
                  startIcon={<ReplayIcon />}
                  onClick={() => {
                    retriedRef.current = false
                    void load()
                  }}
                >
                  {t('messageHistory.video.retry')}
                </ActionButton>
              )}
            </>
          )}
        </Box>
      )}
    </Box>
  )
}
