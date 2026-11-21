import { isNotFoundError } from '../../services/api/axios'

/**
 * 记忆接口的错误文案
 *
 * 404 一律换成本地那一句「未找到」说明。后端刻意不区分「记录不存在」与「不在你当前的
 * 范围里」，操作类接口的 404 也不该把服务端的资源名漏出来。
 */
export function memoryErrorMessage(
  error: unknown,
  options: { notFound: string; fallback: string }
): string {
  if (isNotFoundError(error)) return options.notFound
  return error instanceof Error && error.message ? error.message : options.fallback
}
