const range=(low,high)=>low+Math.random()*(high-low);
const ease=t=>t*t*t*(10+t*(-15+6*t));

export function createIdleGaze() {
  let nextAt=null, glance=null, count=0;
  function reset() {
    nextAt=null;
    glance=null;
  }
  return {
    reset,
    sample(now,enabled) {
      if (!enabled) {reset();return null;}
      if (!glance) {
        if (nextAt===null) nextAt=now+range(38,72);
        if (now<nextAt) return null;
        const sign=Math.random()<.5 ? -1 : 1;
        glance={start:now,yaw:sign*range(3.1,4.7),arm:sign*range(.25,.65),pitch:range(-1.3,1.7),
          turn:range(.85,1.18),hold:range(.65,1.25),back:range(1.08,1.45)};
        nextAt=null;
        count++;
      }
      const t=now-glance.start;
      const returnTime=t-glance.turn-glance.hold;
      if (returnTime>=glance.back) {
        reset();
        nextAt=now+range(38,72);
        return null;
      }
      const mix=t<glance.turn ? ease(t/glance.turn) : 1-ease(Math.max(0,returnTime)/glance.back);
      return {yaw:glance.yaw*mix,arm:glance.arm*mix,pitch:glance.pitch*mix};
    },
    inspect(now) {return {active:glance!==null,count,nextIn:nextAt===null ? null : Math.max(0,nextAt-now)};}
  };
}
