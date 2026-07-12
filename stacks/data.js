// Library payload + heavyweight assets (atlases, HDRI, chair GLB).
// Top-level await: importing this module means the data is ready.

import * as THREE from "three";
import { RGBELoader } from "./vendor/RGBELoader.js";
import { GLTFLoader } from "./vendor/GLTFLoader.js";

const res = await fetch("./assets/library.json");
const data = await res.json();
export const meta = data.meta;
export const games = data.games;
export const FAMILY = meta.familyColors;
export const FAMILY_LABEL = { nintendo: "Nintendo", sony: "PlayStation", xbox: "Xbox", pc: "PC" };

const loadmsg = document.getElementById("loadmsg");
loadmsg.textContent = "loading cover atlases & environment…";
const loader = new THREE.TextureLoader();
const buf = async (url) => (await fetch(url)).arrayBuffer();
// fetch + parse instead of loadAsync: three's FileLoader streaming stalls in
// some headless environments, and plain fetch is equivalent here
const [atlasTextures, hdrBuf, glbBuf] = await Promise.all([
  Promise.all(
    Array.from({ length: meta.sheets }, (_, i) =>
      loader.loadAsync(`./assets/atlas_${i}.jpg`)
    )
  ),
  buf("./assets_static/warehouse_2k.hdr"),
  buf("./assets_static/chair.glb"),
]);
export const atlases = atlasTextures;

loadmsg.textContent = "building environment…";
const hdrData = new RGBELoader().parse(hdrBuf);
export const envTex = new THREE.DataTexture(
  hdrData.data, hdrData.width, hdrData.height, THREE.RGBAFormat, hdrData.type
);
envTex.flipY = true;
envTex.magFilter = THREE.LinearFilter;
envTex.minFilter = THREE.LinearFilter;
envTex.generateMipmaps = false;
envTex.needsUpdate = true;

loadmsg.textContent = "building scene…";
// createImageBitmap-based texture decode stalls in headless browsers; hide it
// during parse so GLTFLoader picks its <img>-element TextureLoader path
const _cib = window.createImageBitmap;
window.createImageBitmap = undefined;
let gltf;
try {
  gltf = await new GLTFLoader().parseAsync(glbBuf, "./assets_static/");
} finally {
  window.createImageBitmap = _cib;
}
export const chairGltf = gltf;

for (const t of atlases) {
  t.minFilter = THREE.LinearMipmapLinearFilter;
  t.magFilter = THREE.LinearFilter;
  t.generateMipmaps = true;
  t.anisotropy = 4;
}
