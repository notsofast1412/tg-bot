const {test} = require('node:test');
const assert = require('node:assert/strict');
const parser = require('../extractor.js');
test('money is exact and rejects malformed/negative amounts', () => {
  assert.equal(parser.money('1 234,56'),123456);
  assert.equal(parser.money('1.001'),null);
  assert.deepEqual(parser.amounts('1.001 ₽'),[]);
  assert.deepEqual(parser.amounts('-1 ₽'),[]);
  assert.deepEqual(parser.amounts('655 ₽ с Ozon Картой; 718 ₽ обычно'),[65500,71800]);
});
test('references reject other sites and preserve WB variant', () => {
  assert.equal(parser.ref('https://www.ozon.ru/product/paint-196525150/').article,'196525150');
  assert.equal(parser.ref('https://www.wildberries.ru/catalog/29220564/detail.aspx?size=2').variant,'2');
  assert.throws(() => parser.ref('https://www.ozon.ru.evil.test/product/1/'));
});
test('Ozon payload excludes private and ordinary-price fields', () => {
  const raw = {personal:'private', widgetStates:{'webStickyProducts-1':{sku:'196525150',name:'Paint',seller:{link:'/seller/shop-123/'},private:'secret'},'webPrice-1':{cardPrice:'655 ₽',price:'718 ₽',secret:'value'},'webUser-1':{name:'private'}}};
  const data = parser.cleanOzon(raw,'196525150');
  assert.equal(data.widgetStates['webPrice-1'].cardPrice,'655 ₽');
  assert.ok(!JSON.stringify(data).includes('private'));
  assert.ok(!JSON.stringify(data).includes('718'));
  assert.equal(parser.cleanOzon(raw,'1'),null);
});
test('WB payload contains only the requested SKU and required size fields', () => {
  const data = parser.cleanWb({products:[{id:1,name:'Other'},{id:29220564,name:'Paint',supplierId:2,sizes:[{optionId:3,price:{product:35000},stocks:[{qty:1,private:'secret'}]}]}]},'29220564');
  assert.equal(data.products.length,1);
  assert.equal(data.products[0].sizes[0].price.product,35000);
  assert.ok(!JSON.stringify(data).includes('secret'));
});

test('card amount accepts only a complete ruble price rather than a discount or mixed text', () => {
  assert.equal(parser.cardAmount('261\u00a0₽'), 26100);
  assert.equal(parser.cardAmount('444 ₽'), 44400);
  assert.equal(parser.cardAmount('1\u202f234,56 ₽'), 123456);
  for (const text of ['-15 ₽', 'Хорошая цена', 'Скидка 15 ₽', '261 ₽ 270 ₽', '15%', '261', '0 ₽']) {
    assert.equal(parser.cardAmount(text), null, text);
  }
});
