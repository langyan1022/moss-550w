// 每次加载只取一次参数。三个关节错峰起停，回程比去程短。
export function createStartupSweep() {
  const range=(low,high)=>low+(high-low)*Math.random();
  const direction=Math.random()<.5 ? -1 : 1;
  const duration=range(2.5,2.85);
  const yaw=direction*range(8,11);
  const pitch=range(4.8,7.2);
  const arm=-direction*range(2.4,3.4);
  const hold=range(.035,.082);
  const ease=t=>{
    t=Math.max(0,Math.min(1,t));
    return t*t*(3-2*t);
  };
  function step(t,start,end) { return ease((t-start)/(end-start)); }
  return {
    duration,
    sample(t) {
      const outbound=step(t,0,.31);
      const cross=step(t,.31+hold,.69);
      const home=step(t,.73,1);
      return {
        yaw:yaw*(outbound-1.43*cross+.43*home),
        arm:arm*(step(t,.04,.33)-step(t,.53,.92)),
        pitch:pitch*(step(t,.15,.45)-step(t,.58,.96)),
        lamp:direction>0 ? 5*t : 5*(1-t),
        lampMix:step(t,0,.085)-step(t,.91,1)
      };
    }
  };
}
