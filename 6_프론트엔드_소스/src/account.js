// 회원·즐겨찾기 API (백엔드/search/accounts_api.md). 세션은 HttpOnly 쿠키라 JS에서 토큰을 다루지 않는다.
const statusMessages = {
  401: '로그인이 필요합니다.',
  403: '요청을 확인하지 못했습니다. 새로고침 후 다시 시도하세요.',
  409: '이미 사용 중입니다.',
  429: '요청이 너무 많습니다. 1분 뒤 다시 시도하세요.',
  503: '회원 서비스에 연결하지 못했습니다. 잠시 후 다시 시도하세요.',
}

export async function memberApi(path, { method = 'GET', body, authorization } = {}) {
  const headers = {
    ...(method === 'GET' ? {} : { 'X-DICE-CSRF': '1' }),
    ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    ...(authorization ? { Authorization: authorization } : {}),
  }
  const response = await fetch(`/api/${path}`, {
    method, headers, credentials: 'same-origin',
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }).catch(() => { throw new Error('서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.') })
  const data = await response.json().catch(() => null)
  if (!response.ok || !data) {
    const error = new Error(data?.error || statusMessages[response.status] || '요청을 처리하지 못했습니다.')
    error.status = response.status
    error.basic = response.headers.has('www-authenticate')  // SEARCH_PASSWORD 공유 인증 요구(회원 세션 만료와 구분)
    throw error
  }
  return data
}

// ponytail: 즐겨찾기 전체를 500건씩 끝까지 읽는다. 수천 건 이상이면 폴더별 지연 조회로 바꾼다.
export async function loadFavorites(authorization) {
  const [{ folders }, rows] = await Promise.all([
    memberApi('favorites/folders', { authorization }),
    (async () => {
      const all = []
      for (let offset = 0; ; offset += 500) {
        const page = await memberApi(`favorites?limit=500&offset=${offset}`, { authorization })
        all.push(...page.favorites)
        if (all.length >= page.total || !page.favorites.length) return all
      }
    })(),
  ])
  return { folders, rows }
}

export function folderName(query) {
  return query.trim().replace(/\s+/g, ' ').slice(0, 100)
}
