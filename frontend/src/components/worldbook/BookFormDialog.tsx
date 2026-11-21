import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import NekroDialog from '../common/NekroDialog'
import { useNotification } from '../../hooks/useNotification'
import {
  worldbookApi,
  type WorldbookBookDetail,
  type WorldbookScopeParams,
} from '../../services/api/worldbook'
import BookFields from './BookFields'
import {
  DEFAULT_WORLDBOOK_BOOK_FORM,
  bookFormFromBook,
  bookFormToCreateRequest,
  bookFormToUpdateRequest,
  validateBookForm,
  type WorldbookBookFormState,
} from './bookForm'
import { useWorldbookScopeOptions } from './useWorldbookScopeOptions'
import { useWorldbookWriteScope } from './useWorldbookWriteScope'

interface BookFormDialogProps {
  open: boolean
  mode: 'create' | 'edit'
  scope: WorldbookScopeParams
  book?: WorldbookBookDetail | null
  onClose: () => void
  onCreated?: (book: WorldbookBookDetail) => void
  onUpdated?: (book: WorldbookBookDetail) => void
}

/**
 * 建书 / 书设置对话框
 *
 * 建书时作用域可改；设置时作用域只读显示。提交只发改过的字段由
 * `bookFormToUpdateRequest` 负责，页面不重复这份比较。
 */
export default function BookFormDialog({
  open,
  mode,
  scope: viewScope,
  book,
  onClose,
  onCreated,
  onUpdated,
}: BookFormDialogProps) {
  // 建书要落在某个实例上；单实例部署下顶栏是全局视图，用唯一的那个实例
  const scope = useWorldbookWriteScope(viewScope)
  const { t } = useTranslation('worldbook')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { data: options } = useWorldbookScopeOptions(scope)

  const [form, setForm] = useState<WorldbookBookFormState>(() =>
    mode === 'edit' && book ? bookFormFromBook(book) : { ...DEFAULT_WORLDBOOK_BOOK_FORM }
  )

  const errors = useMemo(
    () =>
      validateBookForm(form, {
        hasInstance: Boolean(scope.instance_key),
        scopeLocked: mode === 'edit',
      }),
    [form, scope.instance_key, mode]
  )
  const hasErrors = Object.keys(errors).length > 0

  const mutation = useMutation({
    mutationFn: async () => {
      if (mode === 'edit') {
        if (!book) throw new Error(tCommon('messages.operationFailed'))
        const body = bookFormToUpdateRequest(form, book)
        if (Object.keys(body).length === 0) return book
        return worldbookApi.updateBook(book.id, body)
      }
      return worldbookApi.createBook(bookFormToCreateRequest(form), scope)
    },
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: ['worldbook'] })
      if (mode === 'edit') {
        notification.success(t('bookForm.updateSuccess'))
        onUpdated?.(result)
      } else {
        notification.success(t('bookForm.createSuccess'))
        onCreated?.(result)
      }
      onClose()
    },
    onError: (error: unknown) => {
      notification.error(
        error instanceof Error && error.message
          ? error.message
          : tCommon('messages.operationFailed')
      )
    },
  })

  return (
    <NekroDialog
      open={open}
      onClose={onClose}
      title={mode === 'edit' ? t('bookForm.editTitle') : t('bookForm.createTitle')}
      maxWidth="md"
      actions={
        <>
          <ActionButton tone="ghost" onClick={onClose} disabled={mutation.isPending}>
            {tCommon('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone="primary"
            onClick={() => mutation.mutate()}
            disabled={hasErrors || mutation.isPending}
          >
            {mode === 'edit' ? tCommon('actions.save') : tCommon('actions.create')}
          </ActionButton>
        </>
      }
    >
      <BookFields
        value={form}
        onChange={setForm}
        errors={errors}
        mode={mode}
        scope={scope}
        presets={options?.presets ?? []}
        channels={options?.channels ?? []}
        book={book ?? undefined}
      />
    </NekroDialog>
  )
}
