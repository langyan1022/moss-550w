/**
 * Comprehensive China (All 34 Provincial-Level Divisions, Prefecture Cities & Major Districts)
 * + International Major Global Cities Coordinate Catalog (WGS-84 decimal degrees).
 *
 * Compact tuple format: [zhName, enName, lat, lon]
 */

const RAW_REGIONS = [
  // ── 中国 34 个省级行政区 ──
  {
    region: '北京市', en: 'Beijing', group: 'cn', code: 'CN-BJ',
    cities: [
      ['北京', 'Beijing', 39.9042, 116.4074],
      ['东城', 'Dongcheng', 39.9284, 116.4163],
      ['西城', 'Xicheng', 39.9123, 116.3659],
      ['朝阳', 'Chaoyang', 39.9215, 116.4431],
      ['海淀', 'Haidian', 39.9599, 116.2981],
      ['丰台', 'Fengtai', 39.8585, 116.2863],
      ['石景山', 'Shijingshan', 39.9056, 116.2229],
      ['通州', 'Tongzhou', 39.9099, 116.6564],
      ['昌平', 'Changping', 40.2207, 116.2312],
      ['大兴', 'Daxing', 39.7269, 116.3414],
      ['顺义', 'Shunyi', 40.1301, 116.6542],
    ],
  },
  {
    region: '天津市', en: 'Tianjin', group: 'cn', code: 'CN-TJ',
    cities: [
      ['天津', 'Tianjin', 39.0842, 117.2010],
      ['和平', 'Heping', 39.1171, 117.2146],
      ['河西', 'Hexi', 39.1095, 117.2233],
      ['南开', 'Nankai', 39.1382, 117.1507],
      ['滨海新区', 'Binhai', 39.0031, 117.7109],
      ['武清', 'Wuqing', 39.3842, 117.0444],
    ],
  },
  {
    region: '上海市', en: 'Shanghai', group: 'cn', code: 'CN-SH',
    cities: [
      ['上海', 'Shanghai', 31.2304, 121.4737],
      ['黄浦', 'Huangpu', 31.2317, 121.4844],
      ['徐汇', 'Xuhui', 31.1883, 121.4368],
      ['静安', 'Jingan', 31.2279, 121.4474],
      ['长宁', 'Changning', 31.2204, 121.4246],
      ['浦东新区', 'Pudong', 31.2215, 121.5444],
      ['闵行', 'Minhang', 31.1128, 121.3817],
      ['嘉定', 'Jiading', 31.3747, 121.2655],
      ['松江', 'Songjiang', 31.0322, 121.2277],
      ['宝山', 'Baoshan', 31.4045, 121.4896],
    ],
  },
  {
    region: '重庆市', en: 'Chongqing', group: 'cn', code: 'CN-CQ',
    cities: [
      ['重庆', 'Chongqing', 29.5630, 106.5516],
      ['渝中', 'Yuzhong', 29.5527, 106.5690],
      ['江北', 'Jiangbei', 29.6067, 106.5744],
      ['渝北', 'Yubei', 29.7181, 106.6312],
      ['南岸', 'Nanan', 29.5231, 106.5634],
      ['沙坪坝', 'Shapingba', 29.5411, 106.4575],
      ['万州', 'Wanzhou', 30.8078, 108.4086],
      ['涪陵', 'Fuling', 29.7031, 107.3901],
      ['永川', 'Yongchuan', 29.3563, 105.9270],
    ],
  },
  {
    region: '河北省', en: 'Hebei', group: 'cn', code: 'CN-HE',
    cities: [
      ['石家庄', 'Shijiazhuang', 38.0428, 114.5149],
      ['唐山', 'Tangshan', 39.6309, 118.1802],
      ['秦皇岛', 'Qinhuangdao', 39.9354, 119.6005],
      ['邯郸', 'Handan', 36.6256, 114.5390],
      ['邢台', 'Xingtai', 37.0706, 114.5048],
      ['保定', 'Baoding', 38.8739, 115.4646],
      ['张家口', 'Zhangjiakou', 40.8244, 114.8875],
      ['承德', 'Chengde', 40.9515, 117.9634],
      ['沧州', 'Cangzhou', 38.3045, 116.8388],
      ['廊坊', 'Langfang', 39.5380, 116.6838],
      ['衡水', 'Hengshui', 37.7390, 115.6702],
      ['雄安新区', 'Xiongan', 39.0510, 115.9320],
    ],
  },
  {
    region: '山西省', en: 'Shanxi', group: 'cn', code: 'CN-SX',
    cities: [
      ['太原', 'Taiyuan', 37.8706, 112.5489],
      ['大同', 'Datong', 40.0768, 113.3001],
      ['阳泉', 'Yangquan', 37.8567, 113.5805],
      ['长治', 'Changzhi', 36.1954, 113.1163],
      ['晋城', 'Jincheng', 35.4909, 112.8513],
      ['朔州', 'Shuozhou', 39.3316, 112.4333],
      ['晋中', 'Jinzhong', 37.6870, 112.7527],
      ['运城', 'Yuncheng', 35.0264, 111.0070],
      ['忻州', 'Xinzhou', 38.4167, 112.7342],
      ['临汾', 'Linfen', 36.0880, 111.5190],
      ['吕梁', 'Lvliang', 37.5183, 111.1443],
    ],
  },
  {
    region: '内蒙古自治区', en: 'Inner Mongolia', group: 'cn', code: 'CN-NM',
    cities: [
      ['呼和浩特', 'Hohhot', 40.8415, 111.7519],
      ['包头', 'Baotou', 40.6574, 109.8404],
      ['乌海', 'Wuhai', 39.6550, 106.7942],
      ['赤峰', 'Chifeng', 42.2578, 118.8869],
      ['通辽', 'Tongliao', 43.6525, 122.2442],
      ['鄂尔多斯', 'Ordos', 39.6083, 109.7813],
      ['呼伦贝尔', 'Hulunbuir', 49.2116, 119.7657],
      ['巴彦淖尔', 'Bayannur', 40.7432, 107.3877],
      ['乌兰察布', 'Ulanqab', 40.9948, 113.1326],
      ['兴安盟', 'Hinggan', 46.0776, 122.0377],
      ['锡林郭勒盟', 'Xilingol', 43.9334, 116.0482],
      ['阿拉善盟', 'Alxa', 38.8519, 105.7289],
    ],
  },
  {
    region: '辽宁省', en: 'Liaoning', group: 'cn', code: 'CN-LN',
    cities: [
      ['沈阳', 'Shenyang', 41.8057, 123.4315],
      ['大连', 'Dalian', 38.9140, 121.6147],
      ['鞍山', 'Anshan', 41.1086, 122.9946],
      ['抚顺', 'Fushun', 41.8809, 123.9572],
      ['本溪', 'Benxi', 41.2942, 123.7665],
      ['丹东', 'Dandong', 40.0005, 124.3547],
      ['锦州', 'Jinzhou', 41.1074, 121.1270],
      ['营口', 'Yingkou', 40.6670, 122.2354],
      ['阜新', 'Fuxin', 42.0216, 121.6703],
      ['辽阳', 'Liaoyang', 41.2673, 123.2370],
      ['盘锦', 'Panjin', 41.1199, 122.0707],
      ['铁岭', 'Tieling', 42.2230, 123.7262],
      ['朝阳', 'Chaoyang', 41.5737, 120.4504],
      ['葫芦岛', 'Huludao', 40.7111, 120.8369],
    ],
  },
  {
    region: '吉林省', en: 'Jilin', group: 'cn', code: 'CN-JL',
    cities: [
      ['长春', 'Changchun', 43.8171, 125.3235],
      ['吉林', 'Jilin', 43.8379, 126.5496],
      ['四平', 'Siping', 43.1664, 124.3504],
      ['辽源', 'Liaoyuan', 42.8879, 125.1435],
      ['通化', 'Tonghua', 41.7284, 125.9397],
      ['白山', 'Baishan', 41.9399, 126.4278],
      ['松原', 'Songyuan', 45.1418, 124.8251],
      ['白城', 'Baicheng', 45.6196, 122.8390],
      ['延边', 'Yanbian', 42.8913, 129.5089],
    ],
  },
  {
    region: '黑龙江省', en: 'Heilongjiang', group: 'cn', code: 'CN-HL',
    cities: [
      ['哈尔滨', 'Harbin', 45.8038, 126.5350],
      ['齐齐哈尔', 'Qiqihar', 47.3543, 123.9182],
      ['鸡西', 'Jixi', 45.2951, 130.9693],
      ['鹤岗', 'Hegang', 47.3499, 130.2977],
      ['双鸭山', 'Shuangyashan', 46.6469, 131.1595],
      ['大庆', 'Daqing', 46.5893, 125.1046],
      ['伊春', 'Yichun', 47.7275, 128.8407],
      ['佳木斯', 'Jiamusi', 46.7999, 130.3189],
      ['七台河', 'Qitaihe', 45.7713, 131.0031],
      ['牡丹江', 'Mudanjiang', 44.5517, 129.6332],
      ['黑河', 'Heihe', 50.2451, 127.5283],
      ['绥化', 'Suihua', 46.6538, 126.9689],
      ['大兴安岭', 'Daxinganling', 50.4191, 124.1190],
    ],
  },
  {
    region: '江苏省', en: 'Jiangsu', group: 'cn', code: 'CN-JS',
    cities: [
      ['南京', 'Nanjing', 32.0603, 118.7969],
      ['无锡', 'Wuxi', 31.4912, 120.3119],
      ['徐州', 'Xuzhou', 34.2044, 117.2841],
      ['常州', 'Changzhou', 31.8112, 119.9741],
      ['苏州', 'Suzhou', 31.2989, 120.5853],
      ['南通', 'Nantong', 31.9802, 120.8943],
      ['连云港', 'Lianyungang', 34.5967, 119.2216],
      ['淮安', 'Huaian', 33.6104, 119.0153],
      ['盐城', 'Yancheng', 33.3474, 120.1636],
      ['扬州', 'Yangzhou', 32.3942, 119.4129],
      ['镇江', 'Zhenjiang', 32.1878, 119.4258],
      ['泰州', 'Taizhou', 32.4558, 119.9231],
      ['宿迁', 'Suqian', 33.9632, 118.2752],
      ['昆山', 'Kunshan', 31.3856, 120.9807],
    ],
  },
  {
    region: '浙江省', en: 'Zhejiang', group: 'cn', code: 'CN-ZJ',
    cities: [
      ['杭州', 'Hangzhou', 30.2741, 120.1551],
      ['宁波', 'Ningbo', 29.8683, 121.5440],
      ['温州', 'Wenzhou', 27.9938, 120.6994],
      ['嘉兴', 'Jiaxing', 30.7461, 120.7555],
      ['湖州', 'Huzhou', 30.8943, 120.0868],
      ['绍兴', 'Shaoxing', 30.0302, 120.5802],
      ['金华', 'Jinhua', 29.0791, 119.6474],
      ['衢州', 'Quzhou', 28.9358, 118.8595],
      ['舟山', 'Zhoushan', 29.9855, 122.2072],
      ['台州', 'Taizhou', 28.6564, 121.4208],
      ['丽水', 'Lishui', 28.4676, 119.9228],
      ['义乌', 'Yiwu', 29.3068, 120.0750],
    ],
  },
  {
    region: '安徽省', en: 'Anhui', group: 'cn', code: 'CN-AH',
    cities: [
      ['合肥', 'Hefei', 31.8206, 117.2272],
      ['芜湖', 'Wuhu', 31.3529, 118.4329],
      ['蚌埠', 'Bengbu', 32.9163, 117.3897],
      ['淮南', 'Huainan', 32.6255, 116.9999],
      ['马鞍山', 'Maanshan', 31.6705, 118.5068],
      ['淮北', 'Huaibei', 33.9558, 116.7983],
      ['铜陵', 'Tongling', 30.9445, 117.8121],
      ['安庆', 'Anqing', 30.5429, 117.0635],
      ['黄山', 'Huangshan', 29.7147, 118.3375],
      ['滁州', 'Chuzhou', 32.2557, 118.3335],
      ['阜阳', 'Fuyang', 32.8901, 115.8142],
      ['宿州', 'Suzhou AH', 33.6461, 116.9641],
      ['六安', 'Luan', 31.7359, 116.5197],
      ['亳州', 'Bozhou', 33.8446, 115.7787],
      ['池州', 'Chizhou', 30.6648, 117.4916],
      ['宣城', 'Xuancheng', 30.9407, 118.7589],
    ],
  },
  {
    region: '福建省', en: 'Fujian', group: 'cn', code: 'CN-FJ',
    cities: [
      ['福州', 'Fuzhou', 26.0745, 119.2965],
      ['厦门', 'Xiamen', 24.4798, 118.0894],
      ['莆田', 'Putian', 25.4541, 119.0078],
      ['三明', 'Sanming', 26.2634, 117.6387],
      ['泉州', 'Quanzhou', 24.8741, 118.6757],
      ['漳州', 'Zhangzhou', 24.5130, 117.6471],
      ['南平', 'Nanping', 26.6418, 118.1777],
      ['龙岩', 'Longyan', 25.0751, 117.0177],
      ['宁德', 'Ningde', 26.6657, 119.5479],
    ],
  },
  {
    region: '江西省', en: 'Jiangxi', group: 'cn', code: 'CN-JX',
    cities: [
      ['南昌', 'Nanchang', 28.6829, 115.8582],
      ['景德镇', 'Jingdezhen', 29.2688, 117.1784],
      ['萍乡', 'Pingxiang', 27.6229, 113.8543],
      ['九江', 'Jiujiang', 29.7051, 116.0019],
      ['新余', 'Xinyu', 27.8178, 114.9174],
      ['鹰潭', 'Yingtan', 28.2602, 117.0692],
      ['赣州', 'Ganzhou', 25.8318, 114.9350],
      ['吉安', 'Jian', 27.1130, 114.9929],
      ['宜春', 'Yichun JX', 27.8156, 114.4168],
      ['抚州', 'Fuzhou JX', 27.9492, 116.3584],
      ['上饶', 'Shangrao', 28.4549, 117.9434],
    ],
  },
  {
    region: '山东省', en: 'Shandong', group: 'cn', code: 'CN-SD',
    cities: [
      ['济南', 'Jinan', 36.6512, 117.1201],
      ['青岛', 'Qingdao', 36.0671, 120.3826],
      ['淄博', 'Zibo', 36.8135, 118.0550],
      ['枣庄', 'Zaozhuang', 34.8105, 117.3237],
      ['东营', 'Dongying', 37.4348, 118.6747],
      ['烟台', 'Yantai', 37.4638, 121.4479],
      ['潍坊', 'Weifang', 36.7069, 119.1618],
      ['济宁', 'Jining', 35.4149, 116.5871],
      ['泰安', 'Taian', 36.2003, 117.0876],
      ['威海', 'Weihai', 37.5131, 122.1204],
      ['日照', 'Rizhao', 35.4164, 119.5269],
      ['临沂', 'Linyi', 35.1047, 118.3564],
      ['德州', 'Dezhou', 37.4341, 116.3575],
      ['聊城', 'Liaocheng', 36.4567, 115.9854],
      ['滨州', 'Binzhou', 37.3820, 117.9707],
      ['菏泽', 'Heze', 35.2338, 115.4807],
    ],
  },
  {
    region: '河南省', en: 'Henan', group: 'cn', code: 'CN-HA',
    cities: [
      ['郑州', 'Zhengzhou', 34.7466, 113.6253],
      ['开封', 'Kaifeng', 34.7973, 114.3076],
      ['洛阳', 'Luoyang', 34.6197, 112.4540],
      ['平顶山', 'Pingdingshan', 33.7662, 113.1928],
      ['安阳', 'Anyang', 36.0976, 114.3928],
      ['鹤壁', 'Hebi', 35.7472, 114.2973],
      ['新乡', 'Xinxiang', 35.3030, 113.9268],
      ['焦作', 'Jiaozuo', 35.2159, 113.2418],
      ['濮阳', 'Puyang', 35.7621, 115.0293],
      ['许昌', 'Xuchang', 34.0355, 113.8526],
      ['漯河', 'Luohe', 33.5814, 114.0165],
      ['三门峡', 'Sanmenxia', 34.7724, 111.2002],
      ['南阳', 'Nanyang', 32.9906, 112.5283],
      ['商丘', 'Shangqiu', 34.4142, 115.6563],
      ['信阳', 'Xinyang', 32.1470, 114.0913],
      ['周口', 'Zhoukou', 33.6261, 114.6970],
      ['驻马店', 'Zhumadian', 33.0115, 114.0223],
      ['济源', 'Jiyuan', 35.0671, 112.6027],
    ],
  },
  {
    region: '湖北省', en: 'Hubei', group: 'cn', code: 'CN-HB',
    cities: [
      ['武汉', 'Wuhan', 30.5928, 114.3055],
      ['黄石', 'Huangshi', 30.1997, 115.0385],
      ['十堰', 'Shiyan', 32.6294, 110.7980],
      ['宜昌', 'Yichang', 30.6920, 111.2865],
      ['襄阳', 'Xiangyang', 32.0090, 112.1224],
      ['鄂州', 'Ezhou', 30.3919, 114.8948],
      ['荆门', 'Jingmen', 31.0354, 112.1993],
      ['孝感', 'Xiaogan', 30.9246, 113.9169],
      ['荆州', 'Jingzhou', 30.3352, 112.2397],
      ['黄冈', 'Huanggang', 30.4539, 114.8723],
      ['咸宁', 'Xianning', 29.8414, 114.3225],
      ['随州', 'Suizhou', 31.6902, 113.3825],
      ['恩施', 'Enshi', 30.2722, 109.4882],
    ],
  },
  {
    region: '湖南省', en: 'Hunan', group: 'cn', code: 'CN-HN',
    cities: [
      ['长沙', 'Changsha', 28.2282, 112.9388],
      ['株洲', 'Zhuzhou', 27.8274, 113.1338],
      ['湘潭', 'Xiangtan', 27.8297, 112.9440],
      ['衡阳', 'Hengyang', 26.8938, 112.5719],
      ['邵阳', 'Shaoyang', 27.2389, 111.4678],
      ['岳阳', 'Yueyang', 29.3571, 113.1289],
      ['常德', 'Changde', 29.0316, 111.6985],
      ['张家界', 'Zhangjiajie', 29.1171, 110.4792],
      ['益阳', 'Yiyang', 28.5539, 112.3552],
      ['郴州', 'Chenzhou', 25.7705, 113.0147],
      ['永州', 'Yongzhou', 26.4203, 111.6134],
      ['怀化', 'Huaihua', 27.5697, 110.0016],
      ['娄底', 'Loudi', 27.6973, 111.9945],
      ['湘西', 'Xiangxi', 28.3119, 109.7392],
    ],
  },
  {
    region: '广东省', en: 'Guangdong', group: 'cn', code: 'CN-GD',
    cities: [
      ['广州', 'Guangzhou', 23.1291, 113.2644],
      ['深圳', 'Shenzhen', 22.5431, 114.0579],
      ['珠海', 'Zhuhai', 22.2710, 113.5767],
      ['汕头', 'Shantou', 23.3541, 116.6819],
      ['佛山', 'Foshan', 23.0215, 113.1214],
      ['韶关', 'Shaoguan', 24.8104, 113.5975],
      ['湛江', 'Zhanjiang', 21.2707, 110.3594],
      ['肇庆', 'Zhaoqing', 23.0472, 112.4651],
      ['江门', 'Jiangmen', 22.5787, 113.0819],
      ['茂名', 'Maoming', 21.6630, 110.9254],
      ['惠州', 'Huizhou', 23.1118, 114.4162],
      ['梅州', 'Meizhou', 24.2886, 116.1222],
      ['汕尾', 'Shanwei', 22.7862, 115.3753],
      ['河源', 'Heyuan', 23.7435, 114.7004],
      ['阳江', 'Yangjiang', 21.8580, 111.9822],
      ['清远', 'Qingyuan', 23.6818, 113.0560],
      ['东莞', 'Dongguan', 23.0207, 113.7518],
      ['中山', 'Zhongshan', 22.5176, 113.3928],
      ['潮州', 'Chaozhou', 23.6570, 116.6226],
      ['揭阳', 'Jieyang', 23.5499, 116.3728],
      ['云浮', 'Yunfu', 22.9150, 112.0445],
    ],
  },
  {
    region: '广西壮族自治区', en: 'Guangxi', group: 'cn', code: 'CN-GX',
    cities: [
      ['南宁', 'Nanning', 22.8170, 108.3665],
      ['柳州', 'Liuzhou', 24.3255, 109.4159],
      ['桂林', 'Guilin', 25.2736, 110.2900],
      ['梧州', 'Wuzhou', 23.4770, 111.2791],
      ['北海', 'Beihai', 21.4812, 109.1199],
      ['防城港', 'Fangchenggang', 21.6869, 108.3547],
      ['钦州', 'Qinzhou', 21.9809, 108.6545],
      ['贵港', 'Guigang', 23.1115, 109.5989],
      ['玉林', 'Yulin GX', 22.6364, 110.1648],
      ['百色', 'Baise', 23.9024, 106.6183],
      ['贺州', 'Hezhou', 24.4036, 111.5667],
      ['河池', 'Hechi', 24.6929, 108.0853],
      ['来宾', 'Laibin', 23.7503, 109.2215],
      ['崇左', 'Chongzuo', 22.3773, 107.3650],
    ],
  },
  {
    region: '海南省', en: 'Hainan', group: 'cn', code: 'CN-HI',
    cities: [
      ['海口', 'Haikou', 20.0440, 110.1999],
      ['三亚', 'Sanya', 18.2528, 109.5119],
      ['三沙', 'Sansha', 16.8325, 112.3338],
      ['曾母暗沙', 'Zengmu Ansha', 3.9700, 112.2800],
      ['儋州', 'Danzhou', 19.5209, 109.5809],
      ['文昌', 'Wenchang', 19.5433, 110.7977],
      ['琼海', 'Qionghai', 19.2584, 110.4746],
      ['万宁', 'Wanning', 18.7953, 110.3888],
    ],
  },
  {
    region: '四川省', en: 'Sichuan', group: 'cn', code: 'CN-SC',
    cities: [
      ['成都', 'Chengdu', 30.5728, 104.0668],
      ['自贡', 'Zigong', 29.3390, 104.7784],
      ['攀枝花', 'Panzhihua', 26.5823, 101.7186],
      ['泸州', 'Luzhou', 28.8718, 105.4423],
      ['德阳', 'Deyang', 31.1269, 104.3979],
      ['绵阳', 'Mianyang', 31.4674, 104.6791],
      ['广元', 'Guangyuan', 32.4354, 105.8434],
      ['遂宁', 'Suining', 30.5328, 105.5929],
      ['内江', 'Neijiang', 29.5802, 105.0584],
      ['乐山', 'Leshan', 29.5521, 103.7656],
      ['南充', 'Nanchong', 30.8378, 106.1107],
      ['眉山', 'Meishan', 30.0754, 103.8485],
      ['宜宾', 'Yibin', 28.7518, 104.6434],
      ['广安', 'Guangan', 30.4560, 106.6332],
      ['达州', 'Dazhou', 31.2096, 107.4680],
      ['雅安', 'Yaan', 29.9805, 103.0133],
      ['巴中', 'Bazhong', 31.8679, 106.7475],
      ['资阳', 'Ziyang', 30.1289, 104.6276],
      ['阿坝', 'Aba', 31.8994, 102.2246],
      ['甘孜', 'Garze', 30.0495, 101.9623],
      ['凉山', 'Liangshan', 27.8816, 102.2673],
    ],
  },
  {
    region: '贵州省', en: 'Guizhou', group: 'cn', code: 'CN-GZ',
    cities: [
      ['贵阳', 'Guiyang', 26.6470, 106.6302],
      ['六盘水', 'Liupanshui', 26.5927, 104.8304],
      ['遵义', 'Zunyi', 27.7256, 106.9274],
      ['安顺', 'Anshun', 26.2531, 105.9476],
      ['毕节', 'Bijie', 27.2985, 105.3050],
      ['铜仁', 'Tongren', 27.7315, 109.1896],
      ['黔西南', 'Qianxinan', 25.0878, 104.9064],
      ['黔东南', 'Qiandongnan', 26.5836, 107.9828],
      ['黔南', 'Qiannan', 26.2540, 107.5221],
    ],
  },
  {
    region: '云南省', en: 'Yunnan', group: 'cn', code: 'CN-YN',
    cities: [
      ['昆明', 'Kunming', 25.0389, 102.7183],
      ['曲靖', 'Qujing', 25.4900, 103.7962],
      ['玉溪', 'Yuxi', 24.3520, 102.5465],
      ['保山', 'Baoshan YN', 25.1121, 99.1618],
      ['昭通', 'Zhaotong', 27.3383, 103.7175],
      ['丽江', 'Lijiang', 26.8550, 100.2278],
      ['普洱', 'Puer', 22.8251, 100.9665],
      ['临沧', 'Lincang', 23.8843, 100.0888],
      ['楚雄', 'Chuxiong', 25.0455, 101.5280],
      ['红河', 'Honghe', 23.3631, 103.3748],
      ['文山', 'Wenshan', 23.3979, 104.2155],
      ['西双版纳', 'Xishuangbanna', 22.0077, 100.7974],
      ['大理', 'Dali', 25.6065, 100.2676],
      ['德宏', 'Dehong', 24.4333, 98.5849],
      ['怒江', 'Nujiang', 25.8175, 98.8567],
      ['迪庆', 'Diqing', 27.8188, 99.7022],
    ],
  },
  {
    region: '西藏自治区', en: 'Tibet', group: 'cn', code: 'CN-XZ',
    cities: [
      ['拉萨', 'Lhasa', 29.6500, 91.1409],
      ['日喀则', 'Shigatse', 29.2669, 88.8806],
      ['昌都', 'Chamdo', 31.1409, 97.1720],
      ['林芝', 'Nyingchi', 29.6491, 94.3615],
      ['山南', 'Shannan', 29.2371, 91.7731],
      ['那曲', 'Nagqu', 31.4762, 92.0512],
      ['阿里', 'Ngari', 32.5010, 80.1058],
    ],
  },
  {
    region: '陕西省', en: 'Shaanxi', group: 'cn', code: 'CN-SN',
    cities: [
      ['西安', 'Xian', 34.3416, 108.9398],
      ['铜川', 'Tongchuan', 34.8967, 108.9451],
      ['宝鸡', 'Baoji', 34.3620, 107.2375],
      ['咸阳', 'Xianyang', 34.3296, 108.7090],
      ['渭南', 'Weinan', 34.4999, 109.5098],
      ['延安', 'Yanan', 36.5854, 109.4896],
      ['汉中', 'Hanzhong', 33.0675, 107.0233],
      ['榆林', 'Yulin SN', 38.2853, 109.7346],
      ['安康', 'Ankang', 32.6848, 109.0293],
      ['商洛', 'Shangluo', 33.8704, 109.9404],
    ],
  },
  {
    region: '甘肃省', en: 'Gansu', group: 'cn', code: 'CN-GS',
    cities: [
      ['兰州', 'Lanzhou', 36.0611, 103.8343],
      ['嘉峪关', 'Jiayuguan', 39.7731, 98.2892],
      ['金昌', 'Jinchang', 38.5201, 102.1880],
      ['白银', 'Baiyin', 36.5448, 104.1376],
      ['天水', 'Tianshui', 34.5809, 105.7249],
      ['武威', 'Wuwei', 37.9283, 102.6380],
      ['张掖', 'Zhangye', 38.9259, 100.4498],
      ['平凉', 'Pingliang', 35.5427, 106.6651],
      ['酒泉', 'Jiuquan', 39.7325, 98.4944],
      ['庆阳', 'Qingyang', 35.7098, 107.6436],
      ['定西', 'Dingxi', 35.5807, 104.6263],
      ['陇南', 'Longnan', 33.4008, 104.9217],
      ['临夏', 'Linxia', 35.6012, 103.2105],
      ['甘南', 'Gannan', 34.9833, 102.9110],
    ],
  },
  {
    region: '青海省', en: 'Qinghai', group: 'cn', code: 'CN-QH',
    cities: [
      ['西宁', 'Xining', 36.6171, 101.7782],
      ['海东', 'Haidong', 36.5022, 102.1033],
      ['海北', 'Haibei', 36.9543, 100.9009],
      ['黄南', 'Huangnan', 35.5195, 102.0152],
      ['海南州', 'Hainan QH', 36.2864, 100.6195],
      ['果洛', 'Golog', 34.4717, 100.2450],
      ['玉树', 'Yushu', 33.0058, 97.0087],
      ['海西', 'Haixi', 37.3771, 97.3698],
    ],
  },
  {
    region: '宁夏回族自治区', en: 'Ningxia', group: 'cn', code: 'CN-NX',
    cities: [
      ['银川', 'Yinchuan', 38.4872, 106.2309],
      ['石嘴山', 'Shizuishan', 38.9843, 106.3830],
      ['吴忠', 'Wuzhong', 37.9975, 106.1982],
      ['固原', 'Guyuan', 36.0159, 106.2426],
      ['中卫', 'Zhongwei', 37.5003, 105.1968],
    ],
  },
  {
    region: '新疆维吾尔自治区', en: 'Xinjiang', group: 'cn', code: 'CN-XJ',
    cities: [
      ['乌鲁木齐', 'Urumqi', 43.8256, 87.6168],
      ['克拉玛依', 'Karamay', 45.5799, 84.8892],
      ['吐鲁番', 'Turpan', 42.9513, 89.1898],
      ['哈密', 'Hami', 42.8185, 93.5149],
      ['昌吉', 'Changji', 44.0112, 87.3082],
      ['博尔塔拉', 'Bortala', 44.9056, 82.0666],
      ['巴音郭楞', 'Bayingolin', 41.7641, 86.1453],
      ['阿克苏', 'Aksu', 41.1688, 80.2606],
      ['克孜勒苏', 'Kizilsu', 39.7146, 76.1678],
      ['喀什', 'Kashgar', 39.4677, 75.9898],
      ['和田', 'Hotan', 37.1142, 79.9222],
      ['伊犁', 'Ili', 43.9169, 81.3241],
      ['塔城', 'Tacheng', 46.7454, 82.9803],
      ['阿勒泰', 'Altay', 47.8449, 88.1413],
    ],
  },
  {
    region: '香港特别行政区', en: 'Hong Kong', group: 'cn', code: 'CN-HK',
    cities: [
      ['香港', 'Hong Kong', 22.3193, 114.1694],
      ['九龙', 'Kowloon', 22.3204, 114.1700],
      ['新界', 'New Territories', 22.4445, 114.1135],
    ],
  },
  {
    region: '澳门特别行政区', en: 'Macau', group: 'cn', code: 'CN-MO',
    cities: [
      ['澳门', 'Macau', 22.1987, 113.5439],
      ['氹仔', 'Taipa', 22.1558, 113.5586],
    ],
  },
  {
    region: '台湾省', en: 'Taiwan', group: 'cn', code: 'CN-TW',
    cities: [
      ['台北', 'Taipei', 25.0330, 121.5654],
      ['新北', 'New Taipei', 25.0120, 121.4657],
      ['桃园', 'Taoyuan', 24.9936, 121.3010],
      ['台中', 'Taichung', 24.1477, 120.6736],
      ['台南', 'Tainan', 22.9999, 120.2269],
      ['高雄', 'Kaohsiung', 22.6273, 120.3014],
      ['基隆', 'Keelung', 25.1276, 121.7392],
      ['新竹', 'Hsinchu', 24.8138, 120.9675],
      ['花莲', 'Hualien', 23.9872, 121.6016],
      ['钓鱼岛', 'Diaoyu Island', 25.7444, 123.4736],
    ],
  },

  // ── 国际主要城市 ──
  {
    region: '亚洲主要城市', en: 'Asia Major Cities', group: 'intl', code: 'INT-AS',
    cities: [
      ['东京', 'Tokyo', 35.6762, 139.6503],
      ['大阪', 'Osaka', 34.6937, 135.5023],
      ['首尔', 'Seoul', 37.5665, 126.9780],
      ['釜山', 'Busan', 35.1796, 129.0756],
      ['新加坡', 'Singapore', 1.3521, 103.8198],
      ['曼谷', 'Bangkok', 13.7563, 100.5018],
      ['吉隆坡', 'Kuala Lumpur', 3.1390, 101.6869],
      ['雅加达', 'Jakarta', -6.2088, 106.8456],
      ['马尼拉', 'Manila', 14.5995, 120.9842],
      ['胡志明市', 'Ho Chi Minh City', 10.8231, 106.6297],
      ['河内', 'Hanoi', 21.0278, 105.8342],
      ['新德里', 'New Delhi', 28.6139, 77.2090],
      ['孟买', 'Mumbai', 19.0760, 72.8777],
      ['班加罗尔', 'Bengaluru', 12.9716, 77.5946],
      ['迪拜', 'Dubai', 25.2048, 55.2708],
      ['利雅得', 'Riyadh', 24.7136, 46.6753],
      ['多哈', 'Doha', 25.2854, 51.5310],
      ['伊斯坦布尔', 'Istanbul', 41.0082, 28.9784],
      ['阿拉木图', 'Almaty', 43.2220, 76.8512],
    ],
  },
  {
    region: '欧洲主要城市', en: 'Europe Major Cities', group: 'intl', code: 'INT-EU',
    cities: [
      ['伦敦', 'London', 51.5072, -0.1276],
      ['巴黎', 'Paris', 48.8566, 2.3522],
      ['柏林', 'Berlin', 52.5200, 13.4050],
      ['法兰克福', 'Frankfurt', 50.1109, 8.6821],
      ['慕尼黑', 'Munich', 48.1351, 11.5820],
      ['苏黎世', 'Zurich', 47.3769, 8.5417],
      ['日内瓦', 'Geneva', 46.2044, 6.1432],
      ['阿姆斯特丹', 'Amsterdam', 52.3676, 4.9041],
      ['布鲁塞尔', 'Brussels', 50.8503, 4.3517],
      ['马德里', 'Madrid', 40.4168, -3.7038],
      ['巴塞罗那', 'Barcelona', 41.3874, 2.1686],
      ['罗马', 'Rome', 41.9028, 12.4964],
      ['米兰', 'Milan', 45.4642, 9.1900],
      ['维也纳', 'Vienna', 48.2082, 16.3738],
      ['布拉格', 'Prague', 50.0755, 14.4378],
      ['华沙', 'Warsaw', 52.2297, 21.0122],
      ['斯德哥尔摩', 'Stockholm', 59.3293, 18.0686],
      ['奥斯陆', 'Oslo', 59.9139, 10.7522],
      ['赫尔辛基', 'Helsinki', 60.1699, 24.9384],
      ['哥本哈根', 'Copenhagen', 55.6761, 12.5683],
      ['莫斯科', 'Moscow', 55.7558, 37.6173],
      ['圣彼得堡', 'Saint Petersburg', 59.9343, 30.3351],
      ['雅典', 'Athens', 37.9838, 23.7275],
      ['里斯本', 'Lisbon', 38.7223, -9.1393],
      ['都柏林', 'Dublin', 53.3498, -6.2603],
    ],
  },
  {
    region: '北美洲主要城市', en: 'North America Major Cities', group: 'intl', code: 'INT-NA',
    cities: [
      ['纽约', 'New York', 40.7128, -74.0060],
      ['洛杉矶', 'Los Angeles', 34.0522, -118.2437],
      ['旧金山', 'San Francisco', 37.7749, -122.4194],
      ['西雅图', 'Seattle', 47.6062, -122.3321],
      ['芝加哥', 'Chicago', 41.8781, -87.6298],
      ['波士顿', 'Boston', 42.3601, -71.0589],
      ['华盛顿', 'Washington D.C.', 38.9072, -77.0369],
      ['休斯敦', 'Houston', 29.7604, -95.3698],
      ['迈阿密', 'Miami', 25.7617, -80.1918],
      ['多伦多', 'Toronto', 43.6532, -79.3832],
      ['温哥华', 'Vancouver', 49.2827, -123.1207],
      ['蒙特利尔', 'Montreal', 45.5017, -73.5673],
      ['墨西哥城', 'Mexico City', 19.4326, -99.1332],
    ],
  },
  {
    region: '南美洲与大洋洲', en: 'South America & Oceania', group: 'intl', code: 'INT-SO',
    cities: [
      ['圣保罗', 'Sao Paulo', -23.5505, -46.6333],
      ['里约热内卢', 'Rio de Janeiro', -22.9068, -43.1729],
      ['布宜诺斯艾利斯', 'Buenos Aires', -34.6037, -58.3816],
      ['圣地亚哥', 'Santiago', -33.4489, -70.6693],
      ['利马', 'Lima', -12.0464, -77.0428],
      ['波哥大', 'Bogota', 4.7110, -74.0721],
      ['悉尼', 'Sydney', -33.8688, 151.2093],
      ['墨尔本', 'Melbourne', -37.8136, 144.9631],
      ['布里斯班', 'Brisbane', -27.4698, 153.0251],
      ['珀斯', 'Perth', -31.9505, 115.8605],
      ['奥克兰', 'Auckland', -36.8509, 174.7645],
    ],
  },
  {
    region: '非洲主要城市', en: 'Africa Major Cities', group: 'intl', code: 'INT-AF',
    cities: [
      ['开罗', 'Cairo', 30.0444, 31.2357],
      ['约翰内斯堡', 'Johannesburg', -26.2041, 28.0473],
      ['开普敦', 'Cape Town', -33.9249, 18.4241],
      ['内罗毕', 'Nairobi', -1.2921, 36.8219],
      ['拉各斯', 'Lagos', 6.5244, 3.3792],
      ['卡萨布兰卡', 'Casablanca', 33.5731, -7.5898],
      ['亚的斯亚贝巴', 'Addis Ababa', 8.9806, 38.7578],
    ],
  },
];

export const REGIONS = RAW_REGIONS.map((r) => ({
  region: r.region,
  en: r.en,
  group: r.group,
  code: r.code,
  count: r.cities.length,
}));

export const CITIES = [];
for (const reg of RAW_REGIONS) {
  reg.cities.forEach(([name, en, lat, lon], idx) => {
    CITIES.push({
      id: `${reg.code}-${String(idx + 1).padStart(2, '0')}`,
      name,
      en,
      region: reg.region,
      regionEn: reg.en,
      group: reg.group,
      code: reg.code,
      lat,
      lon,
    });
  });
}

export function installCityCatalog({ domestic, international }) {
  for (const [group, entries] of [['cn', domestic], ['intl', international]]) {
    if (!Array.isArray(entries)) continue;
    for (const entry of entries) {
      if (CITIES.some(city => city.backendId === entry.id)) continue;
      const region = entry.country || entry.province;
      const name = entry.district ? `${entry.city} · ${entry.district}` : (entry.city || entry.name);
      if (!region || !name || !Number.isFinite(entry.latitude) || !Number.isFinite(entry.longitude)) continue;
      const current = CITIES.find((city) => city.group === group && !city.backendId
        && Math.abs(city.lat - entry.latitude) < 0.06 && Math.abs(city.lon - entry.longitude) < 0.06
        && [entry.name, entry.city, entry.district].some((label) => label &&
          (label === city.name || (entry.district || entry.city).startsWith(city.name))));
      if (current) {
        current.backendId ??= entry.id;
        continue;
      }
      let meta = REGIONS.find((item) => item.region === region && item.group === group);
      if (!meta) {
        meta = { region, en: region, group, code: entry.id, count: 0 };
        REGIONS.push(meta);
      }
      meta.count++;
      CITIES.push({ id: entry.id, backendId: entry.id, name, en: name, region,
        regionEn: meta.en, group, code: meta.code, lat: entry.latitude, lon: entry.longitude });
    }
  }
}

export function getCitiesByRegion(regionName) {
  return CITIES.filter((c) => c.region === regionName);
}

export function findCityById(id) {
  return CITIES.find((c) => c.id === id) || null;
}

export function findCityByName(name) {
  const q = String(name || '').trim().toLowerCase();
  if (!q) return null;
  return CITIES.find((c) => c.name.toLowerCase() === q || c.en.toLowerCase() === q)
    || CITIES.find((c) => c.name.toLowerCase().includes(q) || c.en.toLowerCase().includes(q))
    || null;
}

export function searchCities(query, limit = 14) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return [];
  const exact = [];
  const partial = [];
  for (const c of CITIES) {
    const n = c.name.toLowerCase();
    const e = c.en.toLowerCase();
    const r = c.region.toLowerCase();
    const re = c.regionEn.toLowerCase();
    if (n === q || e === q) exact.push(c);
    else if (n.includes(q) || e.includes(q) || r.includes(q) || re.includes(q)) partial.push(c);
  }
  return [...exact, ...partial].slice(0, limit);
}

export function findNearestCity(lat, lon, pool = CITIES) {
  let best = null;
  let bestD2 = Infinity;
  for (const c of pool) {
    const dLat = c.lat - lat;
    const dLon = (c.lon - lon) * Math.cos(((lat + c.lat) * 0.5 * Math.PI) / 180);
    const d2 = dLat * dLat + dLon * dLon;
    if (d2 < bestD2) {
      bestD2 = d2;
      best = c;
    }
  }
  return { city: best, distDeg: Math.sqrt(bestD2) };
}
