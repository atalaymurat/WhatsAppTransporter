# Liste Yonetimi: Ilerleme ve Yapilacaklar

Bu belge CSV ile WhatsApp gonderim listelerinin mevcut durumunu, yarim kalan islerin nasil ele alinacagini ve gelistirme adimlarini takip eder.

## Mevcut Durum

- [x] CSV secilince gecerli kayitlar onizlemede listelenir; her kisi icin gonderme secimi vardir.
- [x] Gonderim secilmeyen kisiler gorevde atlananlar olarak tutulur ve indirilebilir.
- [x] Ayni telefon ve ayni sablon/mesaj daha once gonderilmisse normal gonderimde filtrelenir.
- [x] CSV formunda acikca secilirse onceki gonderim filtresi asilir. Bu secim yeni bir gonderim kimligi olusturur; ayni liste icindeki tekrarlar ve yarim kalan gorevin tekrari yine engellenir.
- [x] Gonderim gorevleri `jobs.json` dosyasina kaydedilir. Uygulama yeniden acilinca calismakta olan gorev bekliyor durumuna alinir ve kalan alicilardan surer.
- [x] Gonderim denemesi sonucundan once kalici gonderim gecmisine yazilir. Sonucu belirsiz denemeler otomatik olarak tekrar gonderilmez.
- [x] Liste detayinda gonderildi, bekliyor, hata, gecersiz numara, daha once gonderilmis ve atlanan kisiler ayri gorulur.
- [x] Hatalilar tekrar siraya alinabilir; numara duzenlenebilir veya kayit silinebilir. Sorunlu ve atlanan kayitlar CSV olarak indirilebilir.
- [x] Ek CSV yukleyerek mevcut goreve yeni kisiler eklenir; ayni kisi ve mesaj tekrar eklenmez.

## Yapilacaklar

### P1 - Yarim kalan gonderimi guvenle surdurme

- [x] Gorev kartinda son islenen CSV satirini, siradaki kisiyi ve kalan kisi sayisini belirgin goster.
- [x] Uygulama yeniden basladiginda devam eden gorevleri bekliyor durumuna alip hemen surdur; hangi kayittan devam edilecegini arayuzde bildir.
- [x] Gonderimden once devam noktasini ve gonderim rezervasyonunu kalici kaydet. Gorev JSON'unu gecici dosyadan atomik degistir; onceki saglam surumu `.bak` dosyasinda tut ve ana dosya bozuksa yedekten kurtar.
- [ ] Sonuc belirsiz kayitlarda kullaniciya WhatsApp'taki giden mesajla karsilastirabilecegi telefon, mesaj, saat ve gorev bilgisini goster. Kullanici karari verilene kadar otomatik tekrar gonderme.
- [x] Kabul kosulu: uygulama gonderim sirasinda kapatilip acildiginda onceden kesinlesmis kisilere tekrar gondermez; kalanlar gorunur ve kaldigi yerden surer. Yeniden baslatma ve bozuk gorev dosyasindan yedek kurtarma testleri eklendi.

### P1 - Ilerleme ve gonderim zamanini anlama

- [ ] Liste ilerlemesini hem sayi hem yuzde olarak goster: toplam, gonderildi, bekliyor, hata, gecersiz, atlandi.
- [ ] Siradaki gonderimin tahmini zamanini ve gunluk limit nedeniyle bekleme varsa bir sonraki calisma zamanini ayri goster.
- [ ] Gonderim baslangic/bitis zamani, son ilerleme zamani ve duraklatilma nedenini gorev detayinda sakla ve goster.
- [ ] Kabul kosulu: kullanici gorev ekranindan listenin nerede oldugunu, neden bekledigini ve tahmini ne zaman devam edecegini gorebilir.

### P1 - Basarisiz ve belirsiz gonderimleri yonetme

- [x] WhatsApp'in sohbet acilirken gosterdigi gecici "Starting chat / Cancel" penceresini gonderim hatasi sayma; sohbet mesaj kutusu hazir olana kadar bekle.
- [ ] Gecici hata, kalici/gecersiz numara ve sonucu belirsiz gonderimi arayuzde farkli aciklamalarla goster.
- [ ] Tekrar dene islemi sadece secilen hata kayitlarini yeniden siraya alsin; belirsiz sonuc icin kullanicidan ayri onay istesin.
- [ ] Tekrar deneme sayisini, son hata mesajini ve her deneme zamanini alici gecmisinde sakla.
- [ ] Kabul kosulu: tekrar deneme, zaten gonderilmis kaydi atlar; kullanici hangi kaydin neden tekrar denendigini gorebilir.

### P2 - Hatali veya ulasilamayan telefonlari duzeltme

- [ ] CSV onizlemesinde ulke kodu, numara uzunlugu ve normalizasyon sonucunu gonderimden once goster; gecersiz satiri gonderilebilir secimden cikar.
- [ ] WhatsApp'in numarayi tanimamadigi durum ile sohbet acilamadi/ag baglantisi hatasini ayri siniflandir. Teknik hata, numaranin gecersiz oldugu seklinde raporlanmasin.
- [ ] Sorunlu kayitta eski numara, duzeltilmis numara, hata nedeni ve duzeltme zamani gorunsun. Duzeltilen numara icin gonderim tekrari kullanici kontrolunde olsun.
- [ ] Sorunlu kayitlari CSV/Google Kisiler formatinda disari aktarirken hata etiketlerini koru; duzeltilen numarayi kaynak listeye geri aktarilabilir kil.
- [ ] Kabul kosulu: hatali numara sessizce kaybolmaz; kullanici duzeltir, siler veya daha sonra yeniden inceler.

### P2 - Liste yasam dongusu ve arsiv

- [ ] Her CSV yuklemesi icin dosya adi, yukleme zamani, kaynak satir sayisi ve gorev kimligini sakla.
- [ ] Bir gorev icin ilk CSV, sonradan eklenen dosyalar ve secim/atlama kararlarini birbirinden ayirt edilebilir sekilde kaydet.
- [ ] Tamamlanan gorevleri arsivleme ve arsivden ayrintilari/raporu tekrar acma secenegi ekle.
- [ ] Gorev silme islemi ile gonderim gecmisini silme islemini ayri tut; gecmis varsayilan olarak korunmali.
- [ ] Kabul kosulu: kullanici bir listenin nereden geldigini, hangi kisilerin eklendigini/atlandigini ve sonuc raporunu sonradan bulabilir.

### P2 - Gonderim loglarini analiz etme

- [x] Uygulama ve gonderim olaylarini SQLite veritabaninda kalici sakla; loglari panelde ara ve CSV olarak indir.
- [x] Gonderim loglarina olay seviyesi, gorev kimligi ve telefon bilgisini ekle.

## Uygulama Sirasi

1. Yarim kalan gorev icin acik devam noktasi ve belirsiz sonuc ekrani.
2. Gorev ilerleme, kalan sure ve bekleme nedeninin iyilestirilmesi.
3. Hata siniflari ve kontrollu tekrar deneme akisi.
4. Telefon onizleme/duzeltme ve kaynak listeye geri aktarma.
5. Yukleme kaydi ve tamamlanan gorev arsivi.

## Is Kurallari

- Bir telefon + sablon kimligi, sablon duzenlense bile ayni kampanya sayilir; onceki gonderim varsayilan olarak tekrar edilmez.
- "Onceki gonderim filtresini atla" yalnizca kullanici acikca sectiginde uygulanir ve yeni bir gonderim denemesi olarak kaydedilir.
- Sonucu belirsiz gonderim, kullanici karari veya guvenilir WhatsApp dogrulamasi olmadan otomatik tekrar edilmez.
- CSV'deki gonderim secimi, gorev kaydina kopyalanir; sonradan CSV dosyasinin degismesi olusturulmus gorevi degistirmez.
