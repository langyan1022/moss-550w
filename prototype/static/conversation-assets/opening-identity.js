let artwork;

async function loadArtwork() {
  const response = await fetch('/conversation-assets/550w.svg');
  if (!response.ok) throw Error('无法读取 550W 标识');
  const svg = new DOMParser().parseFromString(await response.text(),'image/svg+xml').documentElement;
  const paths = [...svg.querySelectorAll(':scope > path')];
  if (!paths.length) throw Error('550W 标识缺少路径');
  return paths;
}

export async function drawIdentity(mark) {
  artwork ??= loadArtwork().catch(error=>{ artwork = null; throw error; });
  const paths = await artwork;
  mark.replaceChildren(...paths.map(path=>document.importNode(path,true)));
}
