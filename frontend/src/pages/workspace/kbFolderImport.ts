export const KB_CATEGORY_MAX_LENGTH = 64

/**
 * 批量上传并发数，受网络带宽与后端内存限制。取 3：服务端每个上传要把整份文件读进内存，
 * 小内存部署上 5 路 × 单文件上限的峰值太高；几路并发还会分摊上行带宽，单个文件更慢
 */
export const BATCH_UPLOAD_CONCURRENCY = 3

export interface FolderImportMetadata {
  category: string
  sourcePath: string
}

export function getFolderImportMetadata(file: Pick<File, 'name' | 'webkitRelativePath'>): FolderImportMetadata {
  const normalizedPath = (file.webkitRelativePath || file.name)
    .replace(/\\/g, '/')
    .split('/')
    .filter(Boolean)
    .join('/')

  if (!normalizedPath) {
    return {
      category: '',
      sourcePath: file.name,
    }
  }

  const directoryPath = normalizedPath.split('/').slice(0, -1).join('/')

  return {
    category: directoryPath ? `${directoryPath}/` : '',
    sourcePath: normalizedPath,
  }
}

export function findCategoryLengthOverflow<T extends { category: string }>(
  items: T[]
): { item: T; length: number } | null {
  for (const item of items) {
    const length = item.category.length
    if (length > KB_CATEGORY_MAX_LENGTH) {
      return { item, length }
    }
  }
  return null
}
