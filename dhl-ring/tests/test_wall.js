const fs=require("fs"); const {JSDOM}=require("jsdom");
const html=fs.readFileSync(__dirname+"/../booth/wall.html","utf8");
let feed={schema_version:2,event:"electronica-2026",updated_at:new Date().toISOString(),count:0,leaderboard:[]};
function board(n){const r=[];for(let i=0;i<n;i++){const ms=52000+i*700;const m=Math.floor(ms/60000),s=Math.floor(ms%60000/1000),mm=ms%1000;
  r.push({id:"d"+i,rank:i+1,name:"DRIVER "+i,team:"ACME",best_lap_ms:ms,best_lap:`${m}:${String(s).padStart(2,"0")}.${String(mm).padStart(3,"0")}`});}return r;}
function setFeed(rows){rows.sort((a,b)=>a.best_lap_ms-b.best_lap_ms);rows.forEach((e,i)=>e.rank=i+1);
  feed={schema_version:2,event:"electronica-2026",updated_at:new Date().toISOString(),count:rows.length,leaderboard:rows};}
const log=[]; function rec(l,p,d){log.push({l,p});console.log(`  ${p?"PASS":"FAIL"}  ${l}${d?"   "+d:""}`);}
// hard safety timeout so a hang fails loudly instead of stalling CI
const bail=setTimeout(()=>{console.log("\nTIMED OUT");process.exit(2);},25000);

const dom=new JSDOM(html,{runScripts:"dangerously",pretendToBeVisual:true,
  url:"http://127.0.0.1/wall?top=10&hold=250&settle=80",
  beforeParse(w){
    w.fetch=()=>Promise.resolve({json:()=>Promise.resolve(feed)});
    w.CSS={escape:s=>String(s).replace(/[^a-zA-Z0-9_-]/g,"\\$&")};
    Object.defineProperty(w.HTMLElement.prototype,"getBoundingClientRect",{configurable:true,
      value(){return (this.classList&&this.classList.contains("row"))?{height:64,width:1000,top:0,left:0,right:1000,bottom:64}:{height:0,width:0,top:0,left:0,right:0,bottom:0};}});
    Object.defineProperty(w.HTMLElement.prototype,"clientHeight",{configurable:true,
      get(){return this.id==="wrap"?12*64:0;}});
  }});
const w=dom.window; const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const poll=async()=>{await w.eval("poll()");await sleep(30);};
const ty=()=>{const t=w.document.getElementById("rows").style.transform||"translateY(0)";const m=t.match(/-?[\d.]+/);return m?Math.round(parseFloat(m[0])):0;};
const spot=()=>Array.from(w.document.querySelectorAll(".row.spot")).map(r=>r.getAttribute("data-id"));

(async()=>{
  setFeed(board(8)); await sleep(200); await poll();
  rec("first paint, no scroll", ty()===0, "y="+ty());

  setFeed([...feed.leaderboard,{id:"fast",name:"A. FAST",team:"X",best_lap_ms:51000,best_lap:"0:51.000"}]);
  await poll();
  rec("new time inside top N does not scroll", ty()===0, "y="+ty());

  setFeed(board(30)); await poll(); await sleep(120);
  rec("bulk jump to 30 does not parade (stays near top)", ty()===0, "y="+ty());
  const before=ty();
  setFeed([...feed.leaderboard,{id:"slow20",name:"Z. LATE",team:"Y",best_lap_ms:200000,best_lap:"3:20.000"}]);
  await poll(); await sleep(120);
  rec("new time outside top N scrolls down", ty()<0, "y "+before+" -> "+ty());
  rec("scrolled row is spotlit", spot().includes("slow20"), "spot="+JSON.stringify(spot()));

  setFeed([...feed.leaderboard,{id:"slow25",name:"Q. ALSO",team:"Z",best_lap_ms:210000,best_lap:"3:30.000"}]);
  await poll();
  rec("second late arrival does not bounce to top", ty()<0, "y="+ty());

  await sleep(1600);  // let both holds (250ms) + settles (80ms) drain
  rec("returns to top after queue drains", ty()===0, "y="+ty());

  clearTimeout(bail);
  const fail=log.filter(x=>!x.p).length;
  console.log(`\n${log.length} checks, ${fail} failed`);
  process.exit(fail?1:0);
})();
